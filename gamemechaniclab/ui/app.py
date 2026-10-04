"""Desktop UI for Game Mechanic Lab.

This module deliberately has no third-party dependencies.  It provides a
useful, real interactive combat workbench even when the simulation package is
not installed yet, while using a project's simulator automatically whenever a
compatible adapter is available.

The UI/core boundary is the small ``EngineAdapter`` class.  A simulator may
implement any of these methods (missing methods use the deterministic demo
engine): ``run_fights``, ``run_sweep``, ``get_replay``, ``save_experiment``,
and ``load_experiment``.
"""

from __future__ import annotations

import json
import math
import random
import re
import statistics
import time
import importlib
import inspect
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


# ---------------------------------------------------------------------------
# Palette and tiny utilities

BG = "#0b1220"
PANEL = "#111c2e"
PANEL_2 = "#16243a"
PANEL_3 = "#1b2c45"
TEXT = "#e8f0fa"
MUTED = "#9aacc4"
ACCENT = "#63d5ff"
GREEN = "#64df9a"
AMBER = "#f4c56a"
RED = "#ff7088"
PURPLE = "#b99cff"
GRID = "#263a55"
FONT = "Segoe UI"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _item_stat(item: Mapping[str, Any], key: str, default: float = 0.0) -> float:
    """Read a stat from either the compact flat editor or nested catalog data."""
    if key in item:
        return _safe_float(item.get(key), default)
    stats = item.get("stats")
    if isinstance(stats, Mapping):
        aliases = {"crit": "critical", "iframe": "i_frame", "dodge": "dodge_chance"}
        return _safe_float(stats.get(key, stats.get(aliases.get(key, ""), default)), default)
    return default


def _fmt(value: Any, digits: int = 1) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, int) or (isinstance(value, float) and value.is_integer()):
        return str(int(value))
    return f"{_safe_float(value):.{digits}f}"


def _read_json_or_yaml(text: str) -> dict[str, Any]:
    """Parse JSON, then a deliberately small YAML subset.

    YAML is optional here.  The data files emitted by the app are JSON (valid
    YAML), and this parser makes simple ``key: value`` snippets pleasant to
    edit without adding a dependency.  Rich YAML remains accepted by a core
    loader when one is installed.
    """
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {"value": obj}
    except json.JSONDecodeError:
        # PyYAML is optional; when present it gives the editor full nested YAML
        # support, while the small parser below keeps the standard-library-only
        # build useful for flat key/value snippets.
        try:
            import yaml  # type: ignore
            obj = yaml.safe_load(text)
            if isinstance(obj, dict):
                return obj
        except (ImportError, AttributeError, TypeError, ValueError):
            pass
        result: dict[str, Any] = {}
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            key, value = line.split(":", 1)
            key, value = key.strip(), value.strip()
            if value.lower() in ("true", "false"):
                val: Any = value.lower() == "true"
            elif value.lower() in ("null", "none"):
                val = None
            else:
                try:
                    val = float(value) if "." in value else int(value)
                except ValueError:
                    val = value.strip("\"'")
            result[key] = val
        if result:
            return result
        raise ValueError("Enter a JSON object or simple YAML key: value data")


def _json_text(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


def _display_value(value: Any) -> str:
    """Render editor values compactly without losing structured data."""
    if isinstance(value, list):
        if all(isinstance(x, str) for x in value):
            return ", ".join(value)
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, Mapping):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _normalize_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Map core event names to the compact labels used by the replay widgets.

    Original fields are retained, so exporting an experiment never loses the
    canonical ``source_id``/``amount`` event schema.
    """
    row = dict(event)
    row.setdefault("actor", row.get("source_id", ""))
    row.setdefault("target", row.get("target_id", ""))
    row.setdefault("value", row.get("amount", ""))
    row.setdefault("skill", row.get("skill_id", ""))
    row.setdefault("type", row.get("event_type", row.get("type", "Event")))
    return row


def _replay_snapshot(
    events: Sequence[Mapping[str, Any]],
    index: int,
    player_id: str = "player",
    enemy_id: str = "enemy",
    player_max: float = 1000.0,
    enemy_max: float = 10000.0,
) -> dict[str, Any]:
    """Fold a combat event prefix into the state shown by the Arena.

    The Arena and Replay pages intentionally use the same reducer instead of
    maintaining a second combat implementation.  This makes scrubbing and
    playback honest: HP, counters, and the highlighted event are all derived
    from the event stream emitted by the real simulator.  The helper is kept
    Tk-free so adapters and tests can verify replay semantics without opening a
    desktop window.
    """
    rows = [dict(event) for event in events if isinstance(event, Mapping)]
    if not rows:
        return {
            "index": -1,
            "count": 0,
            "time": 0.0,
            "event": {},
            "player_hp": max(0.0, float(player_max)),
            "enemy_hp": max(0.0, float(enemy_max)),
            "damage_dealt": 0.0,
            "damage_taken": 0.0,
            "critical_hits": 0,
            "dodges": 0,
            "skills": 0,
            "combo": 0,
        }
    cursor = max(0, min(int(index), len(rows) - 1))
    player_key = str(player_id or "player").lower()
    enemy_key = str(enemy_id or "enemy").lower()

    def is_player(value: Any) -> bool:
        text = str(value or "").lower()
        return text in {player_key, "player", "p", "hero"}

    def is_enemy(value: Any) -> bool:
        text = str(value or "").lower()
        return text in {enemy_key, "enemy", "e", "boss", "dummy"}

    p_hp, e_hp = max(0.0, float(player_max)), max(0.0, float(enemy_max))
    dealt = taken = 0.0
    crits = dodges = skills = combo = 0
    for event in rows[: cursor + 1]:
        kind = str(event.get("type", event.get("event_type", "")))
        source = event.get("actor", event.get("source_id", ""))
        target = event.get("target", event.get("target_id", ""))
        amount = _safe_float(event.get("value", event.get("amount", 0.0)), 0.0)
        if kind in {"Damage", "DotTick"}:
            if is_player(source) and is_enemy(target):
                e_hp = max(0.0, e_hp - max(0.0, amount))
                dealt += max(0.0, amount)
            elif is_enemy(source) and is_player(target):
                p_hp = max(0.0, p_hp - max(0.0, amount))
                taken += max(0.0, amount)
        elif kind in {"Heal", "HotTick"} and is_player(target or source):
            p_hp = min(float(player_max), p_hp + max(0.0, amount))
        elif kind == "Critical":
            crits += 1
        elif kind == "Dodge":
            dodges += 1
        elif kind == "SkillCast" and is_player(source):
            skills += 1
        elif kind == "ComboStep" and is_player(source):
            combo = max(combo, _safe_int(amount, combo))
    current = rows[cursor]
    return {
        "index": cursor,
        "count": len(rows),
        "time": _safe_float(current.get("time", 0.0)),
        "event": current,
        "player_hp": p_hp,
        "enemy_hp": e_hp,
        "damage_dealt": dealt,
        "damage_taken": taken,
        "critical_hits": crits,
        "dodges": dodges,
        "skills": skills,
        "combo": combo,
    }


# ---------------------------------------------------------------------------
# Data models used by the fallback and form editors


DEFAULT_SKILLS: list[dict[str, Any]] = [
    {"id": "slash", "name": "Arc Slash", "damage": 34, "multiplier": 1.15, "cooldown": 1.1, "cast_time": 0.1, "recovery": 0.25, "range": 2.2, "cost": 0, "status": ""},
    {"id": "pierce", "name": "Piercing Shot", "damage": 42, "multiplier": 1.35, "cooldown": 2.6, "cast_time": 0.25, "recovery": 0.35, "range": 8, "projectile_speed": 14, "cost": 8, "status": "bleed"},
    {"id": "guard_break", "name": "Guard Break", "damage": 20, "multiplier": 0.8, "cooldown": 3.5, "cast_time": 0.18, "recovery": 0.3, "range": 2.5, "cost": 10, "status": "armor_down"},
    {"id": "nova", "name": "Nova Burst", "damage": 58, "multiplier": 1.7, "cooldown": 6.0, "cast_time": 0.5, "recovery": 0.4, "range": 5, "aoe": 3, "cost": 25, "status": "stun"},
    {"id": "second_wind", "name": "Second Wind", "damage": 0, "multiplier": 0, "cooldown": 12, "cast_time": 0.2, "recovery": 0.2, "range": 0, "cost": 20, "status": "hot"},
]

DEFAULT_BUILDS: list[dict[str, Any]] = [
    {"id": "balanced", "name": "Balanced Vanguard", "hp": 1000, "attack": 120, "defense": 35, "crit": 0.15, "attack_speed": 1.0, "move_speed": 4.5, "mana": 100, "stamina": 100, "skills": ["slash", "pierce", "guard_break", "nova", "second_wind"]},
    {"id": "glass_cannon", "name": "Glass Cannon", "hp": 720, "attack": 175, "defense": 12, "crit": 0.35, "attack_speed": 1.25, "move_speed": 5.4, "mana": 120, "stamina": 80, "skills": ["slash", "pierce", "nova"]},
    {"id": "bulwark", "name": "Bulwark", "hp": 1450, "attack": 92, "defense": 68, "crit": 0.08, "attack_speed": 0.82, "move_speed": 3.6, "mana": 80, "stamina": 130, "skills": ["slash", "guard_break", "second_wind"]},
]

DEFAULT_ENEMIES: list[dict[str, Any]] = [
    {"id": "training_dummy", "name": "Training Dummy", "hp": 900, "attack": 30, "defense": 15, "move_speed": 0, "ai_style": "Passive", "resistance": 0, "phase": 1},
    {"id": "duelist", "name": "Clockwork Duelist", "hp": 1400, "attack": 82, "defense": 32, "move_speed": 4.0, "ai_style": "Aggressive", "resistance": 0.1, "phase": 1},
    {"id": "warden", "name": "Ash Warden", "hp": 2500, "attack": 105, "defense": 58, "move_speed": 2.6, "ai_style": "Defensive", "resistance": 0.25, "phase": 2},
    {"id": "boss", "name": "The Balance Engine", "hp": 10000, "attack": 135, "defense": 72, "move_speed": 2.0, "ai_style": "Phase", "resistance": 0.2, "phase": 3},
]


@dataclass
class FightResult:
    fight_id: int
    won: bool
    ttk: float
    dps: float
    damage_taken: float
    skills_used: int
    deaths: int = 0


# ---------------------------------------------------------------------------
# Deterministic fallback simulation (real event loop, not just a calculator)


class DemoEngine:
    """Small deterministic combat engine used until a project's core loads."""

    def __init__(self) -> None:
        self.skills = [dict(s) for s in DEFAULT_SKILLS]
        self.enemy_skills: list[dict[str, Any]] = []
        self.builds = [dict(b) for b in DEFAULT_BUILDS]
        self.enemies = [dict(e) for e in DEFAULT_ENEMIES]
        # Source checkouts include richer catalogs; use them for the fallback
        # too so headless/portable runs exercise the same 3-build + boss demo.
        root = Path(__file__).resolve().parents[2]
        try:
            from gamemechaniclab.core.data import load_project
            shared_project = load_project(root)

            def load(name: str, key: str) -> list[dict[str, Any]]:
                doc = shared_project.get(name, {}) if isinstance(shared_project, Mapping) else {}
                if isinstance(doc, Mapping) and isinstance(doc.get(key), list):
                    return [dict(x) for x in doc[key] if isinstance(x, Mapping)]
                payload = json.loads((root / "configs" / name).read_text(encoding="utf-8"))
                return [dict(x) for x in payload.get(key, []) if isinstance(x, Mapping)]
            skills = load("skills.json", "skills")
            builds = load("builds.json", "builds")
            enemies = load("enemies.json", "enemies")
            bosses = load("bosses.json", "bosses")
            if skills: self.skills = skills
            if builds: self.builds = builds
            if enemies: self.enemies = enemies
            known = {str(x.get("id")) for x in self.enemies}
            self.enemies.extend(x for x in bosses if str(x.get("id")) not in known)
            for item in self.builds + self.enemies:
                if isinstance(item.get("stats"), Mapping):
                    for key, value in item["stats"].items(): item.setdefault(key, value)
                    item.setdefault("crit", item.get("critical", item["stats"].get("critical_chance", 0)))
                if "resistance" not in item and isinstance(item.get("resistances"), Mapping):
                    item["resistance"] = item["resistances"]
            for build in self.builds:
                build.setdefault("bot", "optimal")
        except (OSError, ValueError, TypeError):
            pass
        self.last_results: list[FightResult] = []
        self.last_events: list[dict[str, Any]] = []

    @staticmethod
    def _damage(attack: float, multiplier: float, defense: float, crit: bool = False, resistance: float = 0) -> float:
        base = max(1.0, attack * multiplier - defense)
        if crit:
            base *= 1.75
        return max(0.0, base * (1 - resistance))

    def _fight(self, build: Mapping[str, Any], enemy: Mapping[str, Any], fight_id: int, seed: int, capture: bool = False) -> FightResult:
        rng = random.Random(seed)
        hp = _item_stat(build, "hp", 1000)
        enemy_hp = _item_stat(enemy, "hp", 1000)
        attack = _item_stat(build, "attack", 100)
        defense = _item_stat(build, "defense", 20)
        crit = _item_stat(build, "crit", 0.1)
        speed = max(0.1, _item_stat(build, "attack_speed", 1.0))
        enemy_attack = _item_stat(enemy, "attack", 40)
        enemy_def = _item_stat(enemy, "defense", 20)
        raw_resistance = enemy.get("resistance", enemy.get("resistances", 0))
        resistance = _safe_float(raw_resistance.get("physical", 0) if isinstance(raw_resistance, Mapping) else raw_resistance, 0)
        skill_ids = list(build.get("skills", [])) or ["slash"]
        skill_map = {str(s["id"]): s for s in self.skills}
        time_now, damage_done, damage_taken, uses = 0.0, 0.0, 0.0, 0
        cooldowns: dict[str, float] = {}
        events: list[dict[str, Any]] = []
        enemy_next = 1.0
        while enemy_hp > 0 and hp > 0 and time_now < 180:
            # Select a ready skill; this is a compact optimal-rotation bot.
            chosen = None
            for sid in skill_ids:
                if cooldowns.get(str(sid), 0) <= time_now:
                    chosen = skill_map.get(str(sid))
                    if chosen:
                        break
            if chosen is None:
                time_now += 0.05
            else:
                sid = str(chosen.get("id"))
                cast = _safe_float(chosen.get("cast_time"), 0.1) + _safe_float(chosen.get("recovery"), 0.2)
                time_now += max(0.05, cast / speed)
                cooldowns[sid] = time_now + _safe_float(chosen.get("cooldown"), 1.0) / speed
                is_crit = rng.random() < crit
                amount = self._damage(attack + _safe_float(chosen.get("damage"), 0), max(0.1, _safe_float(chosen.get("multiplier"), 1)), enemy_def, is_crit, resistance)
                # A tiny AOE bonus reflects two nearby targets in the arena; no
                # hidden multiplier is used for ordinary single-target skills.
                amount *= 1 + min(0.2, _safe_float(chosen.get("aoe"), 0) * 0.02)
                enemy_hp -= amount
                damage_done += amount
                uses += 1
                if capture:
                    events.append({"time": round(time_now, 3), "type": "SkillCast", "actor": "Player", "skill": chosen.get("name", sid)})
                    events.append({"time": round(time_now, 3), "type": "Critical" if is_crit else "Damage", "actor": "Player", "value": round(amount, 2), "target": enemy.get("name", "Enemy")})
            # Enemy attack is an independent event stream.
            if time_now >= enemy_next and enemy_hp > 0:
                enemy_next += 1.0
                incoming = max(1.0, enemy_attack - defense) * (0.9 + 0.2 * rng.random())
                # Defensive builds occasionally dodge; this makes I-frame and
                # dodge experiments visible in the replay without magic values.
                if rng.random() < 0.08:
                    if capture:
                        events.append({"time": round(time_now, 3), "type": "Dodge", "actor": "Player", "target": enemy.get("name", "Enemy")})
                else:
                    hp -= incoming
                    damage_taken += incoming
                    if capture:
                        events.append({"time": round(time_now, 3), "type": "Damage", "actor": enemy.get("name", "Enemy"), "value": round(incoming, 2), "target": "Player"})
        won = enemy_hp <= 0 and hp > 0
        ttk = min(time_now, 180.0)
        if capture:
            if won:
                events.append({"time": round(ttk, 3), "type": "Death", "actor": enemy.get("name", "Enemy")})
            elif hp <= 0:
                events.append({"time": round(ttk, 3), "type": "Death", "actor": "Player"})
            self.last_events = events
        return FightResult(fight_id, won, ttk, damage_done / max(ttk, 0.01), damage_taken, uses, int(not won))

    def run_fights(self, config: Mapping[str, Any], runs: int = 100, capture: bool = False) -> dict[str, Any]:
        build = config.get("build") or self.builds[0]
        enemy = config.get("enemy") or self.enemies[-1]
        results = [self._fight(build, enemy, i + 1, _safe_int(config.get("seed"), 42) + i, capture and i == 0) for i in range(max(1, runs))]
        self.last_results = results
        wins = sum(1 for r in results if r.won)
        ttks = [r.ttk for r in results]
        dps = [r.dps for r in results]
        return {
            "results": [asdict(r) for r in results],
            "summary": {
                "runs": len(results),
                "wins": wins,
                "win_rate": wins / len(results),
                "avg_ttk": statistics.fmean(ttks),
                "p95_ttk": sorted(ttks)[min(len(ttks) - 1, int(len(ttks) * 0.95))],
                "avg_dps": statistics.fmean(dps),
                "avg_damage_taken": statistics.fmean(r.damage_taken for r in results),
                "avg_skills": statistics.fmean(r.skills_used for r in results),
                "deaths": sum(r.deaths for r in results),
            },
            "events": list(self.last_events) if capture else [],
        }

    def run_sweep(self, config: Mapping[str, Any], runs: int = 100) -> dict[str, Any]:
        hp_values = config.get("boss_hp_values") or [5000, 7500, 10000, 12500]
        builds = config.get("builds") or self.builds
        cells: list[dict[str, Any]] = []
        for bi, build in enumerate(builds):
            for hp in hp_values:
                enemy = dict(config.get("enemy") or self.enemies[-1])
                enemy["hp"] = hp
                result = self.run_fights({"build": build, "enemy": enemy, "seed": 100 + bi * 1000 + int(hp)}, runs)
                cells.append({"build": build.get("name", build.get("id", "Build")), "boss_hp": hp, **result["summary"]})
        return {"cells": cells, "boss_hp_values": hp_values, "build_names": [b.get("name", b.get("id", "Build")) for b in builds]}

    def get_replay(self) -> list[dict[str, Any]]:
        return list(self.last_events)

    def save_experiment(self, payload: Mapping[str, Any], path: str | Path) -> None:
        Path(path).write_text(_json_text(payload), encoding="utf-8")

    def load_experiment(self, path: str | Path) -> dict[str, Any]:
        return json.loads(Path(path).read_text(encoding="utf-8"))


class EngineAdapter:
    """Resolve a project's core simulator while preserving a stable UI API."""

    def __init__(self) -> None:
        self.demo = DemoEngine()
        self.engine: Any = self._discover()
        self.name = type(self.engine).__name__

    def _discover(self) -> Any:
        candidates = (
            ("gamemechaniclab.core.simulation", "CombatSimulator"),
            ("gamemechaniclab.core", "SimulationEngine"),
            ("gamemechaniclab.simulation", "SimulationEngine"),
            ("gamemechaniclab.simulator", "Simulator"),
            ("gamemechaniclab.engine", "CombatEngine"),
        )
        for module_name, class_name in candidates:
            try:
                module = importlib.import_module(module_name)
                cls = getattr(module, class_name, None)
                if cls:
                    # The bundled core exposes CombatSimulator, whose public
                    # API works with Build/Enemy dataclasses.  Wrap it so the
                    # UI can keep its compact flat editor schema.
                    if class_name == "CombatSimulator":
                        return _CoreSimulationAdapter(module, cls())
                    return cls()
            except Exception:
                continue
        return self.demo

    def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        fn = getattr(self.engine, name, None)
        if callable(fn):
            # Adapters in small experiments commonly expose either
            # ``run_fights(config, runs)`` or ``run_fights(config, runs,
            # capture)``.  Trim optional positional arguments progressively.
            try:
                signature = inspect.signature(fn)
            except (TypeError, ValueError):
                signature = None
            if signature is not None:
                for count in range(len(args), -1, -1):
                    try:
                        signature.bind(*args[:count], **kwargs)
                    except TypeError:
                        continue
                    # TypeError raised *inside* the simulator is intentional
                    # runtime feedback, not a reason to silently switch to
                    # the simplified fallback engine.
                    return fn(*args[:count], **kwargs)
            else:
                return fn(*args, **kwargs)
        return getattr(self.demo, name)(*args, **kwargs)

    def run_fights(self, config: Mapping[str, Any], runs: int, capture: bool = False) -> dict[str, Any]:
        out = self._call("run_fights", config, runs, capture)
        return out if isinstance(out, dict) else self.demo.run_fights(config, runs, capture)

    def run_sweep(self, config: Mapping[str, Any], runs: int) -> dict[str, Any]:
        out = self._call("run_sweep", config, runs)
        return out if isinstance(out, dict) else self.demo.run_sweep(config, runs)

    def get_replay(self) -> list[dict[str, Any]]:
        out = self._call("get_replay")
        return out if isinstance(out, list) else self.demo.get_replay()

    def detect_warnings(self, config: Mapping[str, Any], output: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Return explainable balance diagnostics for the current run."""
        try:
            from gamemechaniclab.core.warnings import detect_balance_warnings
            if isinstance(self.engine, _CoreSimulationAdapter):
                self.engine.skills_catalog = [dict(x) for x in config.get("skills", []) if isinstance(x, Mapping)]
                self.engine.enemy_skills_catalog = [dict(x) for x in config.get("enemy_skills", []) if isinstance(x, Mapping)]
                player = self.engine._build(config.get("build") or {})
                enemy = self.engine._enemy(config.get("enemy") or {})
                return detect_balance_warnings(player, enemy, result=output)
        except Exception:
            pass
        # Dependency-free fallback diagnostics mirror the core warning codes.
        build, enemy = config.get("build") or {}, config.get("enemy") or {}
        warnings: list[dict[str, Any]] = []
        crit = _item_stat(build, "crit", _item_stat(build, "critical", 0))
        if crit > 1: crit /= 100
        if crit >= 0.999:
            warnings.append({"type": "100% Crit", "severity": "high", "message": "Critical chance reaches 100%; every eligible hit is critical."})
        skills = config.get("skills", []) or []
        by_id = {str(s.get("id")): s for s in skills if isinstance(s, Mapping)}
        chosen = [by_id.get(str(s), {}) if isinstance(s, str) else s for s in build.get("skills", [])]
        if any(_safe_float(s.get("cooldown"), 1) <= 0 for s in chosen if isinstance(s, Mapping)):
            warnings.append({"type": "Cooldown Loop", "severity": "high", "message": "A skill can be recast without a meaningful cooldown window."})
        summary = output.get("summary", output)
        if summary.get("win_rate") is not None and _safe_float(summary.get("win_rate")) <= 0:
            warnings.append({"type": "Unkillable Build", "severity": "medium", "message": "The player never defeats the target in the supplied sample."})
        attack = max(1, _item_stat(build, "attack", 1))
        if _safe_float(summary.get("avg_dps"), 0) > attack * 8:
            warnings.append({"type": "DPS Explosion", "severity": "high", "message": "Observed DPS is an extreme multiple of attack power."})
        return warnings


class _CoreSimulationAdapter:
    """Bridge the bundled dataclass simulator to the UI's dictionary API."""

    def __init__(self, module: Any, simulator: Any) -> None:
        self.module = module
        self.simulator = simulator
        self.last_events: list[dict[str, Any]] = []
        self.skills_catalog: list[dict[str, Any]] = []
        self.enemy_skills_catalog: list[dict[str, Any]] = []

    @staticmethod
    def _stats(raw: Mapping[str, Any], enemy: bool = False) -> dict[str, Any]:
        # Editors use a flat, friendly schema while core models use ``stats``.
        aliases = {"crit": "critical", "crit_chance": "critical", "critical_chance": "critical",
                   "attackSpeed": "attack_speed", "moveSpeed": "move_speed", "iFrame": "iframe", "i_frame": "iframe"}
        out: dict[str, Any] = {}
        source = raw.get("stats") if isinstance(raw.get("stats"), Mapping) else raw
        for key, value in dict(source or {}).items():
            out[aliases.get(key, key)] = value
        for key in ("hp", "attack", "defense", "critical", "crit_multiplier", "attack_speed", "move_speed", "iframe", "dodge", "mana", "stamina"):
            # A direct editor value intentionally overrides the catalog's
            # original nested stat (critical for a parameter sweep).
            if key in raw:
                out[key] = raw[key]
        for raw_key, canonical in aliases.items():
            if raw_key in raw:
                out[canonical] = raw[raw_key]
        if "hp" in raw and "max_hp" not in raw:
            out["max_hp"] = raw["hp"]
        return out

    def _skills(self, raw: Mapping[str, Any], catalog: Sequence[Mapping[str, Any]] | None = None) -> list[dict[str, Any]]:
        values = raw.get("skills", []) or []
        source = catalog if catalog is not None else self.skills_catalog
        by_id = {str(x.get("id")): x for x in source if isinstance(x, Mapping)}
        out: list[dict[str, Any]] = []
        for item in values:
            if isinstance(item, str):
                item = by_id.get(item, {"id": item})
            if isinstance(item, Mapping):
                out.append(dict(item))
        return out

    def _build(self, raw: Mapping[str, Any]) -> Any:
        from gamemechaniclab.core.models import Build
        data = dict(raw)
        data["stats"] = self._stats(raw)
        data["skills"] = self._skills(raw, self.skills_catalog)
        return Build.from_dict(data)

    def _enemy(self, raw: Mapping[str, Any]) -> Any:
        from gamemechaniclab.core.models import Enemy
        data = dict(raw)
        data["stats"] = self._stats(raw, enemy=True)
        data["skills"] = self._skills(raw, self.enemy_skills_catalog or self.skills_catalog)
        # Flat resistance in the editor is a physical resistance percentage.
        if "resistance" in raw and not isinstance(raw.get("resistance"), Mapping):
            data["resistance"] = {"physical": _safe_float(raw.get("resistance"))}
        data.setdefault("is_boss", str(raw.get("ai_style", "")).lower() == "phase")
        return Enemy.from_dict(data)

    def run_fights(self, config: Mapping[str, Any], runs: int = 100, capture: bool = False) -> dict[str, Any]:
        from gamemechaniclab.core.models import SimulationConfig
        if isinstance(config.get("skills"), list):
            self.skills_catalog = [dict(x) for x in config.get("skills", []) if isinstance(x, Mapping)]
        if isinstance(config.get("enemy_skills"), list):
            self.enemy_skills_catalog = [dict(x) for x in config.get("enemy_skills", []) if isinstance(x, Mapping)]
        player = self._build(config.get("build") or {})
        enemy = self._enemy(config.get("enemy") or {})
        seed = _safe_int(config.get("seed"), 42)
        count = max(1, int(runs))
        # Batch statistics do not need ten thousand full event logs.  Record a
        # single deterministic sample below for Replay while keeping the sweep
        # memory bounded.
        sim_overrides = dict(config.get("simulation") or {}) if isinstance(config.get("simulation"), Mapping) else {}
        sim_overrides.update({key: config[key] for key in (
            "time_step", "max_time", "arena_width", "starting_distance", "allow_movement",
            "enable_phases", "enable_status_effects", "basic_attack_range", "engagement_distance",
            "dodge_cost", "dodge_recovery", "fast_enemy_exposure",
        ) if key in config})
        sim_overrides.update({"seed": seed, "record_replay": False})
        if config.get("formula"):
            from gamemechaniclab.core.formula import FormulaEngine
            sim_overrides["formula"] = FormulaEngine().compile(str(config["formula"])).expression
        sim_cfg = SimulationConfig.from_dict(sim_overrides)
        batch = self.simulator.run_batch(player, enemy, runs=count, config=sim_cfg, seed=seed, keep_fights=count <= 200)
        data = batch.to_dict(include_fights=True) if hasattr(batch, "to_dict") else dict(batch)
        summary = {
            "runs": data.get("runs", runs), "wins": data.get("wins", 0), "win_rate": data.get("win_rate", 0),
            "avg_ttk": data.get("average_ttk") if data.get("average_ttk") is not None else sim_cfg.max_time,
            "p95_ttk": data.get("p95_ttk") if data.get("p95_ttk") is not None else sim_cfg.max_time,
            "avg_dps": data.get("average_dps", 0), "avg_damage_taken": data.get("average_damage_taken", 0),
            "avg_skills": sum(data.get("skill_usage", {}).values()) / max(1, data.get("runs", runs)), "deaths": data.get("player_deaths", data.get("deaths", 0)),
            "simulation_mode": data.get("simulation_mode", "event_replay"),
        }
        fights = data.get("fights", []) or []
        events: list[dict[str, Any]] = []
        if capture:
            replay_cfg = SimulationConfig.from_dict({**sim_overrides, "record_replay": True})
            sample = self.simulator.simulate_fight(player, enemy, config=replay_cfg, seed=seed)
            events = [_normalize_event(event if isinstance(event, Mapping) else event.to_dict()) for event in (getattr(sample, "events", []) or [])]
        self.last_events = events
        results = []
        for i, fight in enumerate(fights):
            duration = _safe_float(fight.get("duration"), 0)
            results.append({"fight_id": i + 1, "won": bool(fight.get("win")), "ttk": _safe_float(fight.get("ttk"), duration), "dps": _safe_float(fight.get("dps")), "damage_taken": _safe_float(fight.get("damage_taken")), "skills_used": sum(fight.get("skill_usage", {}).values()), "deaths": _safe_int(fight.get("player_deaths", fight.get("deaths")))})
        return {"summary": summary, "results": results, "events": events}

    def run_sweep(self, config: Mapping[str, Any], runs: int = 100) -> dict[str, Any]:
        # Keep the UI's Cartesian sweep behavior even when the core has no
        # optional sweep module.  Each cell still uses the real core combat.
        if isinstance(config.get("skills"), list):
            self.skills_catalog = [dict(x) for x in config.get("skills", []) if isinstance(x, Mapping)]
        if isinstance(config.get("enemy_skills"), list):
            self.enemy_skills_catalog = [dict(x) for x in config.get("enemy_skills", []) if isinstance(x, Mapping)]
        hp_values = config.get("boss_hp_values") or [5000, 7500, 10000, 12500]
        builds = config.get("builds") or []
        cells: list[dict[str, Any]] = []
        for bi, build in enumerate(builds):
            for hp in hp_values:
                enemy = dict(config.get("enemy") or {})
                enemy["hp"] = hp
                # The editor keeps canonical values under ``stats`` as well
                # as flat aliases.  Synchronize both sides so HP sweeps do
                # not retain the original max_hp for phase/threshold math.
                if isinstance(enemy.get("stats"), Mapping):
                    nested_stats = dict(enemy["stats"])
                    nested_stats["hp"] = hp
                    nested_stats["max_hp"] = hp
                    enemy["stats"] = nested_stats
                else:
                    enemy["max_hp"] = hp
                # Preserve every simulation/formula override from the UI
                # request.  A sweep cell is the same real fight as the Arena
                # path; dropping ``simulation`` here used to make Formula
                # Lab and i-frame/engagement settings silently ineffective.
                cell_config = dict(config)
                cell_config.update({
                    "build": build,
                    "enemy": enemy,
                    "seed": 100 + bi * 1000 + int(hp),
                    "skills": self.skills_catalog,
                    "enemy_skills": self.enemy_skills_catalog,
                })
                out = self.run_fights(cell_config, runs, False)
                cells.append({"build": build.get("name", build.get("id", "Build")), "boss_hp": hp, **out["summary"]})
        return {"cells": cells, "boss_hp_values": hp_values, "build_names": [b.get("name", b.get("id", "Build")) for b in builds]}

    def get_replay(self) -> list[dict[str, Any]]:
        return list(self.last_events)


# ---------------------------------------------------------------------------
# Tkinter widgets


class ScrollableFrame(ttk.Frame):
    def __init__(self, master: tk.Misc, **kwargs: Any) -> None:
        super().__init__(master, **kwargs)
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0)
        self.scroll = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self.inner.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scroll.grid(row=0, column=1, sticky="ns")
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.window, width=e.width))


class StatCard(ttk.Frame):
    def __init__(self, master: tk.Misc, title: str, value: str = "—", accent: str = ACCENT, **kwargs: Any) -> None:
        super().__init__(master, style="Card.TFrame", padding=(14, 10), **kwargs)
        ttk.Label(self, text=title.upper(), style="Muted.TLabel").pack(anchor="w")
        self.value_label = ttk.Label(self, text=value, style="Stat.TLabel", foreground=accent)
        self.value_label.pack(anchor="w", pady=(3, 0))

    def set(self, value: Any) -> None:
        self.value_label.configure(text=str(value))


# ---------------------------------------------------------------------------
# Main application


class GameMechanicLabApp(tk.Tk):
    NAV_ITEMS = ("Arena", "Experiments", "Builds", "Skills", "Enemies", "Formula", "Replay")

    def __init__(self, engine: EngineAdapter | None = None) -> None:
        super().__init__()
        self.title("Game Mechanic Lab  ·  游戏机制与数值实验室")
        self.geometry("1420x900")
        self.minsize(1120, 720)
        self.configure(bg=BG)
        self.engine = engine or EngineAdapter()
        self.current_page = "Arena"
        self.last_output: dict[str, Any] = {}
        # Arena playback is a view over the captured event stream.  Keeping
        # this state on the app lets the toolbar, canvas, and event feed stay
        # synchronized while the user scrubs or plays a fight back.
        self.arena_replay_events: list[dict[str, Any]] = []
        self.arena_replay_index = 0
        self.arena_playing = False
        self.arena_after_id: str | None = None
        self.experiment: dict[str, Any] = {
            "id": "ui_experiment_001",
            "name": "Boss HP balance check",
            "hypothesis": "A 10,000 HP boss gives a readable 30–60 second fight.",
            "research_question": "Which rules keep the encounter readable and fair?",
            "runs": 100,
            "seed": 42,
            "config": {},
            "results": {},
            "conclusion": "",
            "status": "draft",
        }
        # Match the core simulator's transparent baseline.  Critical and
        # resistance modifiers are resolved by the event loop after this base
        # expression, so the default does not accidentally double-apply them.
        self.formula_expression = "max(0, attack * multiplier + base_damage - defense)"
        self.formula_valid = True
        self.skills = [dict(x) for x in DEFAULT_SKILLS]
        self.enemy_skills: list[dict[str, Any]] = []
        self.simulation_defaults: dict[str, Any] = {}
        self.mechanics_catalog: dict[str, Any] = {}
        self.builds = [dict(x) for x in DEFAULT_BUILDS]
        self.enemies = [dict(x) for x in DEFAULT_ENEMIES]
        self._load_bundled_catalogs()
        self._setup_styles()
        self._build_shell()
        self.show_page("Arena")

    def _load_bundled_catalogs(self) -> None:
        """Load the repository's JSON catalogs when the app is in a source tree.

        The UI remains portable when those files are absent (for example in a
        one-file build), so this is intentionally best-effort.  Flat aliases
        are added alongside nested ``stats`` to keep the compact editors and
        fallback DemoEngine compatible with the data-driven files.
        """
        root = Path(__file__).resolve().parents[2]

        # Share the CLI/core data boundary so compact skill and status IDs are
        # expanded before the editor hands them to the simulator.
        shared_project: dict[str, Any] = {}
        try:
            from gamemechaniclab.core.data import load_project
            loaded = load_project(root)
            if isinstance(loaded, Mapping):
                shared_project = dict(loaded)
        except (ImportError, OSError, RuntimeError, TypeError, ValueError):
            shared_project = {}

        def read(name: str, key: str) -> list[dict[str, Any]] | None:
            try:
                payload = json.loads((root / "configs" / name).read_text(encoding="utf-8"))
                values = payload.get(key, [])
                return [dict(x) for x in values if isinstance(x, Mapping)]
            except (OSError, ValueError, TypeError):
                return None

        loaded_skills = read("skills.json", "skills")
        loaded_builds = read("builds.json", "builds")
        loaded_enemies = read("enemies.json", "enemies")
        loaded_bosses = read("bosses.json", "bosses")
        for key, attr in (("skills", "loaded_skills"), ("builds", "loaded_builds"), ("enemies", "loaded_enemies"), ("bosses", "loaded_bosses")):
            doc = shared_project.get(key)
            values = doc.get(key, []) if isinstance(doc, Mapping) else None
            if isinstance(values, list):
                if attr == "loaded_skills": loaded_skills = [dict(x) for x in values if isinstance(x, Mapping)]
                elif attr == "loaded_builds": loaded_builds = [dict(x) for x in values if isinstance(x, Mapping)]
                elif attr == "loaded_enemies": loaded_enemies = [dict(x) for x in values if isinstance(x, Mapping)]
                else: loaded_bosses = [dict(x) for x in values if isinstance(x, Mapping)]
        try:
            mechanics_doc = shared_project.get("mechanics") if isinstance(shared_project.get("mechanics"), Mapping) else json.loads((root / "configs" / "mechanics.json").read_text(encoding="utf-8"))
            if isinstance(mechanics_doc, Mapping):
                self.mechanics_catalog = dict(mechanics_doc)
            defaults = mechanics_doc.get("simulation_defaults", {})
            if isinstance(defaults, Mapping):
                self.simulation_defaults = dict(defaults)
            formulas = mechanics_doc.get("formulas", {})
            if isinstance(formulas, Mapping) and formulas.get("damage"):
                self.formula_expression = str(formulas["damage"])
        except (OSError, ValueError, TypeError):
            pass
        try:
            enemy_doc = shared_project.get("enemies") if isinstance(shared_project.get("enemies"), Mapping) else json.loads((root / "configs" / "enemies.json").read_text(encoding="utf-8"))
            boss_doc = shared_project.get("bosses") if isinstance(shared_project.get("bosses"), Mapping) else json.loads((root / "configs" / "bosses.json").read_text(encoding="utf-8"))
            values = list((enemy_doc.get("enemy_skills") or {}).values()) + list((boss_doc.get("boss_skills") or {}).values())
            self.enemy_skills = [dict(x) for x in values if isinstance(x, Mapping)]
        except (OSError, ValueError, TypeError):
            pass
        if loaded_skills:
            self.skills = loaded_skills
        if loaded_builds:
            self.builds = loaded_builds
        if loaded_enemies:
            self.enemies = loaded_enemies
        if loaded_bosses:
            known = {str(x.get("id")) for x in self.enemies}
            self.enemies.extend(x for x in loaded_bosses if str(x.get("id")) not in known)
        try:
            demo = json.loads((root / "demo" / "experiment.json").read_text(encoding="utf-8"))
            self.experiment.update({
                "name": demo.get("name", self.experiment["name"]),
                "hypothesis": (demo.get("hypotheses") or [{}])[0].get("statement", self.experiment["hypothesis"]),
                "seed": _safe_int((demo.get("scope") or {}).get("deterministic_seed"), self.experiment["seed"]),
            })
        except (OSError, ValueError, TypeError, IndexError, AttributeError):
            pass
        self._flatten_editor_stats()
        for build in self.builds:
            build.setdefault("bot", "optimal")

    def _flatten_editor_stats(self) -> None:
        """Expose nested catalog stats as editable top-level aliases."""
        for item in self.builds + self.enemies:
            stats = item.get("stats")
            if isinstance(stats, Mapping):
                for key, value in stats.items():
                    item.setdefault(key, value)
                # Common editor aliases make the important fields visible.
                if "critical" not in item and "critical_chance" in stats:
                    item["critical"] = stats["critical_chance"]
                item.setdefault("crit", item.get("critical", stats.get("critical_chance", 0)))
                if "iframe" not in item and "i_frame" in stats:
                    item["iframe"] = stats["i_frame"]
                if "dodge" not in item and "dodge_chance" in stats:
                    item["dodge"] = stats["dodge_chance"]
            if "resistance" not in item and isinstance(item.get("resistances"), Mapping):
                item["resistance"] = item["resistances"]

    # ---- style / shell -------------------------------------------------

    def _setup_styles(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("Card.TFrame", background=PANEL_2, relief="flat")
        style.configure("TLabel", background=BG, foreground=TEXT, font=(FONT, 10))
        style.configure("Muted.TLabel", background=PANEL_2, foreground=MUTED, font=(FONT, 8))
        style.configure("Title.TLabel", background=BG, foreground=TEXT, font=(FONT, 20, "bold"))
        style.configure("Subtitle.TLabel", background=BG, foreground=MUTED, font=(FONT, 9))
        style.configure("Stat.TLabel", background=PANEL_2, foreground=ACCENT, font=(FONT, 17, "bold"))
        style.configure("Section.TLabel", background=PANEL, foreground=TEXT, font=(FONT, 12, "bold"))
        style.configure("Nav.TButton", background=PANEL, foreground=MUTED, padding=(14, 10), anchor="w", font=(FONT, 10))
        style.map("Nav.TButton", background=[("active", PANEL_3)], foreground=[("active", TEXT)])
        style.configure("Accent.TButton", background=ACCENT, foreground="#06111f", padding=(13, 8), font=(FONT, 9, "bold"))
        style.map("Accent.TButton", background=[("active", "#8be4ff")])
        style.configure("Ghost.TButton", background=PANEL_3, foreground=TEXT, padding=(10, 7))
        style.configure("TEntry", fieldbackground="#0d1829", foreground=TEXT, insertcolor=TEXT, bordercolor=GRID, padding=6)
        style.configure("TCombobox", fieldbackground="#0d1829", background="#0d1829", foreground=TEXT, arrowcolor=ACCENT)
        style.configure("Treeview", background="#0d1829", fieldbackground="#0d1829", foreground=TEXT, rowheight=27, bordercolor=GRID)
        style.configure("Treeview.Heading", background=PANEL_3, foreground=TEXT, font=(FONT, 9, "bold"))
        style.map("Treeview", background=[("selected", "#24516d")], foreground=[("selected", "#ffffff")])
        style.configure("TLabelframe", background=PANEL, foreground=TEXT, bordercolor=GRID)
        style.configure("TLabelframe.Label", background=PANEL, foreground=MUTED)
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=PANEL_2, foreground=MUTED, padding=(12, 7))
        style.map("TNotebook.Tab", background=[("selected", PANEL_3)], foreground=[("selected", TEXT)])
        style.configure("TProgressbar", troughcolor="#0d1829", background=ACCENT, bordercolor=GRID, lightcolor=ACCENT, darkcolor=ACCENT)

    def _build_shell(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)
        # Header
        header = ttk.Frame(self, padding=(24, 17, 24, 12))
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        header.grid_columnconfigure(1, weight=1)
        brand = ttk.Frame(header)
        brand.grid(row=0, column=0, sticky="w")
        ttk.Label(brand, text="GAME MECHANIC LAB", style="Title.TLabel").pack(anchor="w")
        ttk.Label(brand, text="游戏机制与数值实验室   /   DATA-DRIVEN COMBAT WORKBENCH", style="Subtitle.TLabel").pack(anchor="w", pady=(2, 0))
        controls = ttk.Frame(header)
        controls.grid(row=0, column=2, sticky="e")
        ttk.Label(controls, text="RUNS", style="Muted.TLabel").grid(row=0, column=0, padx=(0, 5))
        self.runs_var = tk.StringVar(value="100")
        self.runs_combo = ttk.Combobox(controls, textvariable=self.runs_var, values=("1", "100", "10000"), width=7, state="readonly")
        self.runs_combo.grid(row=0, column=1, padx=(0, 8))
        ttk.Button(controls, text="▶  RUN SIMULATION", style="Accent.TButton", command=self.run_simulation).grid(row=0, column=2, padx=3)
        ttk.Button(controls, text="▦  SWEEP", style="Ghost.TButton", command=self.run_sweep).grid(row=0, column=3, padx=3)
        ttk.Button(controls, text="↥  EXPORT", style="Ghost.TButton", command=self.export_experiment).grid(row=0, column=4, padx=(3, 0))

        # Navigation
        self.nav = ttk.Frame(self, style="Panel.TFrame", padding=(10, 14))
        self.nav.grid(row=1, column=0, sticky="nsw")
        ttk.Label(self.nav, text="WORKSPACE", style="Muted.TLabel").pack(anchor="w", padx=10, pady=(3, 9))
        self.nav_buttons: dict[str, ttk.Button] = {}
        for item in self.NAV_ITEMS:
            b = ttk.Button(self.nav, text=f"  {self._nav_icon(item)}   {item}", style="Nav.TButton", command=lambda x=item: self.show_page(x))
            b.pack(fill="x", pady=1)
            self.nav_buttons[item] = b
        ttk.Separator(self.nav).pack(fill="x", pady=16)
        ttk.Label(self.nav, text="DATA", style="Muted.TLabel").pack(anchor="w", padx=10, pady=(0, 8))
        ttk.Button(self.nav, text="  ⇩   Import JSON / YAML", style="Nav.TButton", command=self.import_config).pack(fill="x", pady=1)
        ttk.Button(self.nav, text="  ⌘   Save experiment", style="Nav.TButton", command=self.export_experiment).pack(fill="x", pady=1)
        engine_kind = "CORE ENGINE" if isinstance(self.engine.engine, _CoreSimulationAdapter) else "DEMO ENGINE"
        self.status_label = ttk.Label(self.nav, text=f"●  {engine_kind} READY", foreground=GREEN, style="Muted.TLabel", wraplength=175)
        self.status_label.pack(anchor="w", padx=10, pady=(26, 0))

        # Content
        self.content = ttk.Frame(self, padding=(18, 2, 24, 20))
        self.content.grid(row=1, column=1, sticky="nsew")
        self.content.grid_columnconfigure(0, weight=1)
        # Page title is a compact header; the page body receives the flexible
        # viewport so Arena, tables, and replay timelines actually expand.
        self.content.grid_rowconfigure(0, weight=0)
        self.content.grid_rowconfigure(1, weight=1)

    @staticmethod
    def _nav_icon(item: str) -> str:
        return {"Arena": "◈", "Experiments": "✦", "Builds": "♟", "Skills": "✧", "Enemies": "◆", "Formula": "ƒ", "Replay": "▷"}.get(item, "•")

    def _clear_content(self) -> None:
        self._stop_arena_playback()
        for child in self.content.winfo_children():
            child.destroy()

    @staticmethod
    def _widget_alive(widget: Any) -> bool:
        """Return False for page widgets destroyed during navigation."""
        if widget is None:
            return False
        try:
            return bool(widget.winfo_exists())
        except (AttributeError, tk.TclError):
            return False

    def show_page(self, page: str) -> None:
        self.current_page = page
        self._clear_content()
        for key, button in self.nav_buttons.items():
            button.configure(style="Ghost.TButton" if key == page else "Nav.TButton")
        method = getattr(self, f"_page_{page.lower()}", self._page_arena)
        method()

    # ---- shared sections ------------------------------------------------

    def _page_title(self, title: str, subtitle: str) -> ttk.Frame:
        top = ttk.Frame(self.content)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        top.grid_columnconfigure(0, weight=1)
        ttk.Label(top, text=title, style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(top, text=subtitle, style="Subtitle.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 0))
        return top

    def _panel(self, parent: tk.Misc, title: str, row: int = 0, column: int = 0, **kwargs: Any) -> ttk.Frame:
        # ``place`` is used only for the caption so callers may freely choose
        # pack *or* grid for the panel body without Tk geometry-manager
        # conflicts.  Extra top padding keeps the caption clear of controls.
        columnspan = kwargs.pop("columnspan", 1)
        rowspan = kwargs.pop("rowspan", 1)
        sticky = kwargs.pop("sticky", "nsew")
        padx = kwargs.pop("padx", 5)
        pady = kwargs.pop("pady", 5)
        frame = ttk.Frame(parent, style="Panel.TFrame", padding=(14, 38, 14, 14), **kwargs)
        frame.grid(row=row, column=column, columnspan=columnspan, rowspan=rowspan, sticky=sticky, padx=padx, pady=pady)
        ttk.Label(frame, text=title, style="Section.TLabel").place(x=14, y=9, anchor="nw")
        return frame

    def _table(self, parent: tk.Misc, columns: Sequence[str], headings: Sequence[str] | None = None, height: int = 8) -> ttk.Treeview:
        table = ttk.Treeview(parent, columns=list(columns), show="headings", height=height)
        for i, col in enumerate(columns):
            table.heading(col, text=(headings or columns)[i])
            table.column(col, width=max(80, min(190, 640 // max(1, len(columns)))), anchor="center")
        table.pack(fill="both", expand=True)
        return table

    def _combo_row(self, parent: tk.Misc, label: str, variable: tk.StringVar, values: Sequence[str], row: int, width: int = 20) -> None:
        ttk.Label(parent, text=label, foreground=MUTED).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Combobox(parent, textvariable=variable, values=list(values), width=width, state="readonly").grid(row=row, column=1, sticky="ew", pady=4, padx=(12, 0))

    # ---- Arena ----------------------------------------------------------

    def _page_arena(self) -> None:
        self._page_title("Combat Arena", "Run a real event-driven fight and inspect the state of every combatant.")
        body = ttk.Frame(self.content)
        body.grid(row=1, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=3)
        body.grid_columnconfigure(1, weight=2)
        body.grid_rowconfigure(0, weight=1)
        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew")
        left.grid_rowconfigure(0, weight=1)
        left.grid_columnconfigure(0, weight=1)
        arena_panel = ttk.Frame(left, style="Panel.TFrame", padding=12)
        arena_panel.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)
        arena_panel.grid_rowconfigure(1, weight=1)
        arena_panel.grid_columnconfigure(0, weight=1)
        toolbar = ttk.Frame(arena_panel, style="Panel.TFrame")
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        toolbar.grid_columnconfigure(4, weight=1)
        ttk.Label(toolbar, text="ARENA PREVIEW", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        self.arena_build_var = tk.StringVar(value=self.builds[0]["name"])
        self.arena_enemy_var = tk.StringVar(value=self.enemies[-1]["name"])
        ttk.Label(toolbar, text="BUILD", foreground=MUTED).grid(row=0, column=1, padx=(22, 5))
        ttk.Combobox(toolbar, textvariable=self.arena_build_var, values=[b["name"] for b in self.builds], width=18, state="readonly").grid(row=0, column=2)
        ttk.Label(toolbar, text="TARGET", foreground=MUTED).grid(row=0, column=3, padx=(18, 5))
        ttk.Combobox(toolbar, textvariable=self.arena_enemy_var, values=[e["name"] for e in self.enemies], width=18, state="readonly").grid(row=0, column=4, sticky="w")
        self.arena_canvas = tk.Canvas(arena_panel, bg="#091321", highlightthickness=1, highlightbackground=GRID)
        self.arena_canvas.grid(row=1, column=0, sticky="nsew")
        self.arena_canvas.bind("<Configure>", lambda _e: self._draw_arena())

        # Playback controls make the Arena a small live combat instrument,
        # rather than a screenshot of the last result.  All controls operate
        # on the exact replay emitted by the simulator.
        playback = ttk.Frame(arena_panel, style="Panel.TFrame")
        playback.grid(row=2, column=0, sticky="ew", pady=(9, 0))
        playback.grid_columnconfigure(5, weight=1)
        self.arena_play_button = ttk.Button(playback, text="▶ PLAY", style="Ghost.TButton", command=self._toggle_arena_playback)
        self.arena_play_button.grid(row=0, column=0, padx=(0, 4))
        self.arena_step_button = ttk.Button(playback, text="STEP", style="Ghost.TButton", command=self._step_arena_replay)
        self.arena_step_button.grid(row=0, column=1, padx=4)
        self.arena_reset_button = ttk.Button(playback, text="RESET", style="Ghost.TButton", command=self._reset_arena_replay)
        self.arena_reset_button.grid(row=0, column=2, padx=4)
        ttk.Label(playback, text="EVENT", foreground=MUTED).grid(row=0, column=3, padx=(12, 5))
        self.arena_replay_label = ttk.Label(playback, text="0 / 0", foreground=MUTED, width=14)
        self.arena_replay_label.grid(row=0, column=4, padx=(0, 8), sticky="w")
        self.arena_replay_var = tk.DoubleVar(value=0.0)
        self.arena_replay_scale = ttk.Scale(playback, from_=0, to=0, variable=self.arena_replay_var, orient="horizontal", command=self._on_arena_scrub)
        self.arena_replay_scale.grid(row=0, column=5, sticky="ew", padx=(0, 7))
        self.arena_event_label = ttk.Label(playback, text="No replay", foreground=MUTED, width=24, anchor="e")
        self.arena_event_label.grid(row=0, column=6, sticky="e")
        self._set_arena_replay(self.arena_replay_events, reset=True)
        self._draw_arena()
        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_rowconfigure(1, weight=1)
        cards = ttk.Frame(right)
        cards.grid(row=0, column=0, sticky="ew")
        for i in range(2):
            cards.grid_columnconfigure(i, weight=1)
        self.stat_ttk = StatCard(cards, "WIN RATE", "—", GREEN)
        self.stat_ttk.grid(row=0, column=0, sticky="ew", padx=5, pady=5)
        self.stat_dps = StatCard(cards, "AVG DPS", "—", ACCENT)
        self.stat_dps.grid(row=0, column=1, sticky="ew", padx=5, pady=5)
        self.stat_ttk2 = StatCard(cards, "AVG TTK", "—", AMBER)
        self.stat_ttk2.grid(row=1, column=0, sticky="ew", padx=5, pady=5)
        self.stat_taken = StatCard(cards, "DAMAGE TAKEN", "—", RED)
        self.stat_taken.grid(row=1, column=1, sticky="ew", padx=5, pady=5)
        feed = self._panel(right, "COMBAT EVENT FEED", 1, 0)
        feed.pack_propagate(False)
        feed.configure(height=350)
        self.arena_feed = tk.Text(feed, bg="#0a1525", fg=TEXT, insertbackground=TEXT, relief="flat", font=("Consolas", 9), wrap="none")
        self.arena_feed.pack(fill="both", expand=True)
        self._write_feed("Ready. Choose a build and target, then press RUN SIMULATION.", MUTED)
        warn = self._panel(right, "BALANCE WARNINGS", 2, 0)
        warn.configure(height=118)
        warn.grid_propagate(False)
        self.warning_text = tk.Text(warn, height=3, bg="#0a1525", fg=AMBER, relief="flat", font=(FONT, 9), wrap="word")
        self.warning_text.pack(fill="both", expand=True)
        self.warning_text.insert("1.0", "No warnings yet — run an experiment to inspect pathological mechanics.")
        self.warning_text.configure(state="disabled")

    # ---- Arena replay playback -----------------------------------------

    def _set_arena_replay(self, events: Sequence[Mapping[str, Any]], reset: bool = True) -> None:
        """Install a replay and synchronize every Arena playback widget."""
        self.arena_replay_events = [dict(event) for event in events if isinstance(event, Mapping)]
        if reset:
            self.arena_replay_index = 0
        self.arena_replay_index = max(0, min(self.arena_replay_index, max(0, len(self.arena_replay_events) - 1)))
        scale = getattr(self, "arena_replay_scale", None)
        if self._widget_alive(scale):
            scale.configure(to=max(0, len(self.arena_replay_events) - 1))
            self.arena_replay_var.set(float(self.arena_replay_index))
            scale.configure(state="normal" if self.arena_replay_events else "disabled")
        self._update_arena_replay_labels()

    def _update_arena_replay_labels(self) -> None:
        events = self.arena_replay_events
        count = len(events)
        current = events[self.arena_replay_index] if events and 0 <= self.arena_replay_index < count else {}
        if self._widget_alive(getattr(self, "arena_replay_label", None)):
            self.arena_replay_label.configure(text=f"{self.arena_replay_index + 1 if count else 0} / {count}")
        if self._widget_alive(getattr(self, "arena_event_label", None)):
            kind = str(current.get("type", "No replay"))
            timestamp = _safe_float(current.get("time", 0.0)) if current else 0.0
            self.arena_event_label.configure(text=f"{timestamp:.2f}s · {kind}" if current else "No replay")

    def _on_arena_scrub(self, value: str) -> None:
        if not self.arena_replay_events:
            return
        self.arena_replay_index = max(0, min(int(round(_safe_float(value))), len(self.arena_replay_events) - 1))
        self._update_arena_replay_labels()
        self._draw_arena()

    def _toggle_arena_playback(self) -> None:
        if not self.arena_replay_events:
            return
        if self.arena_playing:
            self._stop_arena_playback()
            return
        if self.arena_replay_index >= len(self.arena_replay_events) - 1:
            self.arena_replay_index = 0
        self.arena_playing = True
        if self._widget_alive(getattr(self, "arena_play_button", None)):
            self.arena_play_button.configure(text="Ⅱ PAUSE")
        self._advance_arena_replay()

    def _stop_arena_playback(self) -> None:
        self.arena_playing = False
        after_id = getattr(self, "arena_after_id", None)
        if after_id is not None:
            try:
                self.after_cancel(after_id)
            except (tk.TclError, ValueError):
                pass
            self.arena_after_id = None
        if self._widget_alive(getattr(self, "arena_play_button", None)):
            self.arena_play_button.configure(text="▶ PLAY")

    def _advance_arena_replay(self) -> None:
        if not self.arena_playing or not self.arena_replay_events:
            return
        if self.arena_replay_index >= len(self.arena_replay_events) - 1:
            self._stop_arena_playback()
            return
        previous = self.arena_replay_events[self.arena_replay_index]
        self.arena_replay_index += 1
        current = self.arena_replay_events[self.arena_replay_index]
        self._update_arena_replay_labels()
        self._draw_arena()
        # Preserve relative event timing while keeping very dense event logs
        # readable.  A 4x visual speed-up makes a typical 30–60 s fight fit
        # into a short inspection without skipping event ordering.
        delta = max(0.05, _safe_float(current.get("time", 0.0)) - _safe_float(previous.get("time", 0.0)))
        delay_ms = max(35, min(550, int(delta * 250)))
        self.arena_after_id = self.after(delay_ms, self._advance_arena_replay)

    def _step_arena_replay(self) -> None:
        if not self.arena_replay_events:
            return
        self._stop_arena_playback()
        self.arena_replay_index = min(self.arena_replay_index + 1, len(self.arena_replay_events) - 1)
        self._update_arena_replay_labels()
        self._draw_arena()

    def _reset_arena_replay(self) -> None:
        self._stop_arena_playback()
        self.arena_replay_index = 0
        try:
            self.arena_replay_var.set(0.0)
        except (AttributeError, tk.TclError):
            pass
        self._update_arena_replay_labels()
        self._draw_arena()

    def _draw_arena(self, result: Mapping[str, Any] | None = None) -> None:
        if not self._widget_alive(getattr(self, "arena_canvas", None)):
            return
        c = self.arena_canvas
        c.delete("all")
        w, h = max(500, c.winfo_width()), max(300, c.winfo_height())
        # grid/floor
        for x in range(0, w, 40):
            c.create_line(x, 0, x, h, fill="#102238")
        for y in range(0, h, 40):
            c.create_line(0, y, w, y, fill="#102238")
        c.create_text(24, 22, text="EVENT-DRIVEN 2D COMBAT", anchor="w", fill=MUTED, font=(FONT, 9, "bold"))
        bx, ex = w * 0.28, w * 0.72
        cy = h * 0.52
        build_var = self.__dict__.get("arena_build_var")
        enemy_var = self.__dict__.get("arena_enemy_var")
        build_name = build_var.get() if build_var is not None else self.builds[0].get("name", "")
        enemy_name = enemy_var.get() if enemy_var is not None else self.enemies[-1].get("name", "")
        selected_build = next((b for b in self.builds if b.get("name") == build_name), self.builds[0])
        selected_enemy = next((e for e in self.enemies if e.get("name") == enemy_name), self.enemies[-1])
        # Replay-derived state keeps the Arena honest after a run: the bars,
        # counters, and impact marker reflect the same event stream shown in
        # the feed, rather than resetting to a decorative full-health mockup.
        player_max = _item_stat(selected_build, "hp", 1000)
        enemy_max = _item_stat(selected_enemy, "hp", 10000)
        replay = list(self.arena_replay_events)
        if result is not None and result.get("events") is not None and not replay:
            replay = [dict(event) for event in result.get("events", []) if isinstance(event, Mapping)]
        snapshot = _replay_snapshot(
            replay,
            self.arena_replay_index,
            str(selected_build.get("id", "player")),
            str(selected_enemy.get("id", "enemy")),
            player_max,
            enemy_max,
        )
        player_hp, enemy_hp = snapshot["player_hp"], snapshot["enemy_hp"]
        current_event = snapshot.get("event", {})
        current_kind = str(current_event.get("type", ""))
        if replay:
            self._update_arena_replay_labels()
        # stylized player / dummy, clearly not a calculator-only screen
        player_outline = AMBER if current_kind in {"Damage", "Critical", "Hit", "Dodge"} and str(current_event.get("target", "")).lower() in {str(selected_build.get("id", "")).lower(), "player", "p"} else ACCENT
        enemy_outline = AMBER if current_kind in {"Damage", "Critical", "Hit", "AttackStarted"} and str(current_event.get("target", "")).lower() in {str(selected_enemy.get("id", "")).lower(), "enemy", "e", "boss", "dummy"} else RED
        c.create_oval(bx - 38, cy - 38, bx + 38, cy + 38, fill="#1f779b", outline=player_outline, width=3 if player_outline == AMBER else 2)
        c.create_rectangle(bx - 13, cy - 62, bx + 13, cy - 38, fill=ACCENT, outline="")
        c.create_text(bx, cy, text="P", fill="white", font=(FONT, 20, "bold"))
        c.create_oval(ex - 42, cy - 42, ex + 42, cy + 42, fill="#7b2e49", outline=enemy_outline, width=3 if enemy_outline == AMBER else 2)
        c.create_rectangle(ex - 15, cy - 68, ex + 15, cy - 42, fill=RED, outline="")
        c.create_text(ex, cy, text="E", fill="white", font=(FONT, 20, "bold"))
        # HP bars
        self._bar(c, bx - 80, cy + 65, 160, 10, player_hp, player_max, GREEN, selected_build["name"])
        self._bar(c, ex - 90, cy + 70, 180, 10, enemy_hp, enemy_max, RED, selected_enemy["name"])
        c.create_line(bx + 58, cy, ex - 58, cy, fill="#2d4562", dash=(5, 6), width=2)
        c.create_text(w / 2, cy - 12, text="VS", fill=AMBER, font=(FONT, 15, "bold"))
        if current_kind in {"Damage", "Critical", "Hit", "AttackStarted", "Dodge", "Knockback"}:
            source = str(current_event.get("actor", current_event.get("source_id", ""))).lower()
            from_player = source in {str(selected_build.get("id", "")).lower(), "player", "p", "hero"}
            start_x, end_x = (bx + 43, ex - 48) if from_player else (ex - 43, bx + 48)
            impact_color = AMBER if current_kind in {"Critical", "Dodge"} else RED
            line_options: dict[str, Any] = {"fill": impact_color, "width": 4, "arrow": "last"}
            if current_kind == "Dodge":
                line_options["dash"] = (8, 4)
            c.create_line(start_x, cy, end_x, cy, **line_options)
        if replay:
            label = f"t = {snapshot['time']:.2f}s   ·   {snapshot['index'] + 1}/{snapshot['count']}   ·   {current_kind or 'Event'}"
            c.create_text(w / 2, cy + 112, text=label, fill=AMBER, font=(FONT, 10, "bold"))
            c.create_text(w / 2, cy + 130, text=f"DAMAGE {snapshot['damage_dealt']:.0f}   ·   TAKEN {snapshot['damage_taken']:.0f}   ·   CRITS {snapshot['critical_hits']}   ·   DODGES {snapshot['dodges']}   ·   COMBO {snapshot['combo']}", fill=MUTED, font=(FONT, 8))

    @staticmethod
    def _bar(c: tk.Canvas, x: float, y: float, width: float, height: float, value: float, maximum: float, color: str, label: str) -> None:
        c.create_rectangle(x, y, x + width, y + height, fill="#263447", outline="")
        c.create_rectangle(x, y, x + width * max(0.0, min(1.0, value / max(maximum, 1))), y + height, fill=color, outline="")
        c.create_text(x, y + height + 12, text=f"{label}  {value:.0f}/{maximum:.0f}", anchor="w", fill=MUTED, font=(FONT, 8))

    def _write_feed(self, text: str, color: str = TEXT) -> None:
        if not self._widget_alive(getattr(self, "arena_feed", None)):
            return
        self.arena_feed.insert("end", text + "\n")
        self.arena_feed.tag_configure(color, foreground=color)
        self.arena_feed.tag_add(color, "end-2l", "end-1l")
        self.arena_feed.see("end")

    # ---- Experiments ----------------------------------------------------

    def _page_experiments(self) -> None:
        self._page_title("Experiments", "Define a hypothesis, choose the scope, and retain reproducible runs and conclusions.")
        body = ttk.Frame(self.content)
        body.grid(row=1, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=2)
        body.grid_rowconfigure(1, weight=1)
        form = self._panel(body, "EXPERIMENT BRIEF", 0, 0)
        form.grid_columnconfigure(1, weight=1)
        self.exp_name_var = tk.StringVar(value=self.experiment["name"])
        self.exp_hyp_var = tk.StringVar(value=self.experiment["hypothesis"])
        self.exp_seed_var = tk.StringVar(value=str(self.experiment["seed"]))
        self.exp_conclusion_var = tk.StringVar(value=str(self.experiment.get("conclusion", "")))
        self._entry_row(form, "Name", self.exp_name_var, 0)
        self._entry_row(form, "Hypothesis", self.exp_hyp_var, 1)
        self._entry_row(form, "Seed", self.exp_seed_var, 2)
        self._entry_row(form, "Conclusion", self.exp_conclusion_var, 3)
        ttk.Label(form, text="Status", foreground=MUTED).grid(row=4, column=0, sticky="w", pady=4)
        self.exp_status = ttk.Label(form, text="Draft", foreground=AMBER)
        self.exp_status.grid(row=4, column=1, sticky="w", pady=4)
        ttk.Button(form, text="Save brief", style="Ghost.TButton", command=self._save_brief).grid(row=5, column=1, sticky="e", pady=(10, 0))
        history = self._panel(body, "RUN HISTORY", 0, 1)
        self.exp_history = self._table(history, ("id", "name", "runs", "win_rate", "avg_ttk", "status"), ("ID", "EXPERIMENT", "RUNS", "WIN RATE", "AVG TTK", "STATUS"), 6)
        self.exp_history.insert("", "end", values=("EXP-001", self.experiment["name"], "—", "—", "—", "Draft"))
        hypothesis = self._panel(body, "LOOP", 1, 0, columnspan=2)
        hypothesis.configure(height=170)
        loop = [
            ("01", "Research question", "How much boss HP preserves a fair fight?"),
            ("02", "Hypothesis", "10,000 HP → 30–60 second TTK"),
            ("03", "Experiment", "3 builds × 4 HP values × 100 fights"),
            ("04", "Analysis", "Compare win rate, DPS, TTK, deaths"),
            ("05", "Next step", "Tune defense and phase transitions"),
        ]
        for i, (num, title, value) in enumerate(loop):
            ttk.Label(hypothesis, text=num, foreground=ACCENT, font=(FONT, 11, "bold")).grid(row=0, column=i, padx=18, sticky="w")
            ttk.Label(hypothesis, text=title, foreground=TEXT, font=(FONT, 9, "bold")).grid(row=1, column=i, padx=18, sticky="w")
            ttk.Label(hypothesis, text=value, foreground=MUTED, wraplength=180).grid(row=2, column=i, padx=18, sticky="nw")
            if i < len(loop) - 1:
                ttk.Label(hypothesis, text="→", foreground=AMBER, font=(FONT, 16)).grid(row=1, column=i, padx=(0, 0), sticky="e")

    def _entry_row(self, parent: ttk.Frame, label: str, variable: tk.StringVar, row: int) -> None:
        ttk.Label(parent, text=label, foreground=MUTED).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=variable, width=36).grid(row=row, column=1, sticky="ew", pady=4, padx=(12, 0))

    def _save_brief(self) -> None:
        self.experiment.update({
            "name": self.exp_name_var.get(),
            "hypothesis": self.exp_hyp_var.get(),
            "seed": _safe_int(self.exp_seed_var.get(), 42),
            "conclusion": self.exp_conclusion_var.get(),
        })
        self.exp_status.configure(text="Saved", foreground=GREEN)

    # ---- editor pages ---------------------------------------------------

    def _page_builds(self) -> None:
        self._page_editor("Builds", "Compose player attributes, equipment loadout, skill rotation, passive effects, and bot policy.", self.builds, ("id", "name", "hp", "attack", "defense", "critical", "crit_multiplier", "attack_speed", "move_speed", "cooldown_reduction", "mana", "stamina", "iframe", "dodge", "bot", "equipment", "skills", "passives", "formula"))

    def _page_skills(self) -> None:
        self._page_editor("Skills", "Data-driven skill definitions; no combat value is hard-coded into the interface.", self.skills, ("id", "name", "damage", "multiplier", "cooldown", "cast_time", "recovery", "range", "projectile_speed", "aoe", "cost", "cost_type", "status_effects", "target", "damage_type", "hits", "hitstun", "knockback", "iframe"))

    def _page_enemies(self) -> None:
        self._page_editor("Enemies", "Tune enemy AI, resistances, skill kits, and HP/time/event boss phase thresholds.", self.enemies, ("id", "name", "hp", "attack", "defense", "move_speed", "ai_style", "resistance", "skills", "phases", "formula"))

    def _page_editor(self, title: str, subtitle: str, items: list[dict[str, Any]], columns: Sequence[str]) -> None:
        self._page_title(title, subtitle)
        body = ttk.Frame(self.content)
        body.grid(row=1, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=3)
        body.grid_columnconfigure(1, weight=2)
        body.grid_rowconfigure(0, weight=1)
        list_panel = self._panel(body, f"{title.upper()} CATALOG", 0, 0)
        table = self._table(list_panel, columns, height=14)
        for item in items:
            values = []
            for col in columns:
                val = item.get(col, "")
                values.append(_display_value(val))
            table.insert("", "end", values=values)
        table.bind("<<TreeviewSelect>>", lambda _e: self._populate_editor(table, items, columns, fields_panel))
        buttons = ttk.Frame(list_panel)
        buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(buttons, text="＋ New", style="Ghost.TButton", command=lambda: self._new_item(table, items, columns)).pack(side="left")
        ttk.Button(buttons, text="Duplicate", style="Ghost.TButton", command=lambda: self._duplicate_item(table, items, columns)).pack(side="left", padx=5)
        ttk.Button(buttons, text="Delete", style="Ghost.TButton", command=lambda: self._delete_item(table, items)).pack(side="left")
        fields_panel = self._panel(body, "INSPECTOR", 0, 1)
        fields_panel.grid_columnconfigure(1, weight=1)
        self.editor_vars: dict[str, tk.StringVar] = {}
        self.editor_table = table
        self.editor_items = items
        self.editor_columns = columns
        self.editor_fields_panel = fields_panel
        # Keep the inspector heading and action button stable while only the
        # form body is rebuilt when a different row is selected.
        self.editor_form_host = ttk.Frame(fields_panel, style="Panel.TFrame")
        self.editor_form_host.pack(fill="both", expand=True)
        self._render_editor_fields(self.editor_form_host, items[0] if items else {}, columns)
        ttk.Button(fields_panel, text="Apply changes", style="Accent.TButton", command=lambda: self._apply_editor(table, items, columns)).pack(anchor="e", pady=(14, 0))

    def _render_editor_fields(self, panel: ttk.Frame, item: Mapping[str, Any], columns: Sequence[str]) -> None:
        for child in panel.winfo_children():
            child.destroy()
        form = ttk.Frame(panel, style="Panel.TFrame")
        form.pack(fill="both", expand=True, anchor="n")
        form.grid_columnconfigure(1, weight=1)
        self.editor_vars = {}
        for row, col in enumerate(columns):
            var = tk.StringVar(value=_display_value(item.get(col, "")))
            self.editor_vars[col] = var
            ttk.Label(form, text=col, foreground=MUTED).grid(row=row, column=0, sticky="w", pady=3)
            ttk.Entry(form, textvariable=var).grid(row=row, column=1, sticky="ew", padx=(10, 0), pady=3)
        ttk.Label(form, text="Lists such as skills accept comma-separated IDs.", foreground=MUTED, wraplength=260).grid(row=len(columns), column=0, columnspan=2, sticky="w", pady=(12, 0))

    def _selected_index(self, table: ttk.Treeview) -> int | None:
        selected = table.selection()
        if not selected:
            return None
        return table.index(selected[0])

    def _populate_editor(self, table: ttk.Treeview, items: list[dict[str, Any]], columns: Sequence[str], panel: ttk.Frame) -> None:
        idx = self._selected_index(table)
        if idx is not None and idx < len(items):
            self._render_editor_fields(self.editor_form_host, items[idx], columns)

    def _new_item(self, table: ttk.Treeview, items: list[dict[str, Any]], columns: Sequence[str]) -> None:
        item = {col: (f"new_{len(items)+1}" if col == "id" else ("New Item" if col == "name" else "0")) for col in columns}
        items.append(item)
        vals = [_display_value(item[c]) for c in columns]
        iid = table.insert("", "end", values=vals)
        table.selection_set(iid)
        self._render_editor_fields(self.editor_form_host, item, columns)

    def _duplicate_item(self, table: ttk.Treeview, items: list[dict[str, Any]], columns: Sequence[str]) -> None:
        idx = self._selected_index(table)
        if idx is None:
            return
        item = dict(items[idx])
        item["id"] = f"{item.get('id', 'item')}_copy"
        item["name"] = f"{item.get('name', 'Item')} Copy"
        items.append(item)
        table.insert("", "end", values=[_display_value(item[c]) for c in columns])

    def _delete_item(self, table: ttk.Treeview, items: list[dict[str, Any]]) -> None:
        idx = self._selected_index(table)
        if idx is not None:
            if len(items) <= 1:
                messagebox.showinfo("Catalog", "Keep at least one item in the catalog.")
                return
            del items[idx]
            table.delete(table.selection()[0])

    def _apply_editor(self, table: ttk.Treeview, items: list[dict[str, Any]], columns: Sequence[str]) -> None:
        idx = self._selected_index(table)
        if idx is None:
            return
        item = items[idx]
        for col in columns:
            raw = self.editor_vars.get(col, tk.StringVar()).get()
            old = item.get(col)
            if isinstance(old, list):
                try:
                    parsed = json.loads(raw)
                    item[col] = parsed if isinstance(parsed, list) else [str(parsed)]
                except (ValueError, TypeError):
                    item[col] = [x.strip() for x in raw.split(",") if x.strip()]
            elif isinstance(old, Mapping):
                try:
                    parsed = json.loads(raw)
                    item[col] = parsed if isinstance(parsed, Mapping) else old
                except (ValueError, TypeError):
                    item[col] = old
            elif isinstance(old, bool):
                item[col] = raw.lower() == "true"
            elif isinstance(old, int):
                item[col] = _safe_int(raw, old)
            elif isinstance(old, float):
                item[col] = _safe_float(raw, old)
            else:
                # Preserve numeric columns in newly-created entries where the
                # temporary value is the string "0".
                item[col] = _safe_float(raw) if re.fullmatch(r"-?\d+(?:\.\d+)?", raw.strip()) else raw
        self._sync_editor_stats(item, columns)
        table.item(table.selection()[0], values=[_display_value(item[c]) for c in columns])
        self.status_label.configure(text="●  CATALOG UPDATED", foreground=GREEN)
        if hasattr(self, "arena_build_var"):
            self._draw_arena()

    @staticmethod
    def _sync_editor_stats(item: dict[str, Any], columns: Sequence[str]) -> None:
        """Keep compact editor aliases and canonical nested stats in sync."""
        stats = item.get("stats")
        if not isinstance(stats, dict):
            return
        aliases = {
            "critical": ("critical", "critical_chance", "crit"),
            "crit": ("crit", "critical", "critical_chance"),
            "iframe": ("iframe", "i_frame"),
            "i_frame": ("i_frame", "iframe"),
            "dodge": ("dodge", "dodge_chance"),
            "dodge_chance": ("dodge_chance", "dodge"),
            "critical_multiplier": ("critical_multiplier", "crit_multiplier"),
            "crit_multiplier": ("crit_multiplier", "critical_multiplier"),
            "critical_chance_multiplier": ("critical_chance_multiplier",),
            "move_speed": ("move_speed", "moveSpeed"),
            "attack_speed": ("attack_speed", "attackSpeed"),
            "cooldown_reduction": ("cooldown_reduction", "cooldownReduction"),
        }
        stat_fields = {
            "hp", "max_hp", "attack", "defense", "critical", "critical_chance",
            "crit", "crit_multiplier", "critical_multiplier", "critical_chance_multiplier",
            "attack_speed", "move_speed", "cooldown_reduction", "mana", "max_mana",
            "mana_regen", "stamina", "max_stamina", "stamina_regen", "iframe", "i_frame",
            "dodge", "dodge_chance", "knockback", "hitstun", "knockback_resistance",
            "hitstun_resistance", "shield_received", "damage_below_half_hp",
        }
        for column in columns:
            if column not in stat_fields or column not in item:
                continue
            candidates = aliases.get(column, (column,))
            target = next((key for key in candidates if key in stats), candidates[0])
            stats[target] = item[column]
        if "hp" in columns and "max_hp" in stats and "max_hp" not in columns:
            stats["max_hp"] = item.get("hp", stats["max_hp"])

    # ---- Formula --------------------------------------------------------

    def _page_formula(self) -> None:
        self._page_title("Formula Lab", "Change the damage model safely, then test it against live synthetic fights.")
        body = ttk.Frame(self.content)
        body.grid(row=1, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)
        left = self._panel(body, "SAFE FORMULA EDITOR", 0, 0)
        ttk.Label(left, text="Allowed variables: attack, base_damage, multiplier, defense, critical_multiplier, resistance, is_critical", foreground=MUTED, wraplength=460).pack(anchor="w", pady=(0, 8))
        self.formula_text = tk.Text(left, height=5, bg="#0a1525", fg=ACCENT, insertbackground=TEXT, relief="flat", font=("Consolas", 12), padx=10, pady=10)
        self.formula_text.pack(fill="x")
        self.formula_text.insert("1.0", self.formula_expression)
        controls = ttk.Frame(left, style="Panel.TFrame")
        controls.pack(fill="x", pady=10)
        ttk.Button(controls, text="Validate", style="Ghost.TButton", command=self.validate_formula).pack(side="left")
        ttk.Button(controls, text="Apply + run simulation", style="Accent.TButton", command=self.apply_formula).pack(side="left", padx=7)
        self.formula_status = ttk.Label(controls, text="Ready", foreground=MUTED)
        self.formula_status.pack(side="left", padx=10)
        examples = ttk.Frame(left, style="Panel.TFrame", padding=(10, 8, 10, 8))
        examples.pack(fill="x", pady=(12, 0))
        ttk.Label(examples, text="QUICK CHECK", style="Section.TLabel").pack(anchor="w", pady=(0, 8))
        self.formula_check = self._table(examples, ("attack", "multiplier", "defense", "crit", "result"), ("ATTACK", "MULT", "DEFENSE", "CRIT", "DAMAGE"), 5)
        self._populate_formula_table()
        right = self._panel(body, "FORMULA SAFETY", 0, 1)
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)
        ttk.Label(right, text="The parser accepts arithmetic, comparisons, and a small allow-list of math functions. It never executes Python code.", foreground=MUTED, wraplength=420).grid(row=0, column=0, sticky="w", pady=(0, 10))
        safety = tk.Text(right, bg="#0a1525", fg=GREEN, relief="flat", font=("Consolas", 10), padx=10, pady=10)
        safety.grid(row=1, column=0, sticky="nsew")
        safety.insert("1.0", "SAFE NODES\n──────────\n✓ numbers and named variables\n✓ +  -  *  /  **\n✓ comparisons and if/else\n✓ max(), min(), abs(), round()\n\nBLOCKED\n───────\n✕ imports\n✕ attribute access\n✕ function calls outside allow-list\n✕ Python statements\n✕ filesystem / network access")
        safety.configure(state="disabled")

    def _formula_eval(self, expression: str, values: Mapping[str, Any]) -> float:
        # Keep the editor and headless simulator on the same restricted AST
        # implementation.  The local visitor below remains a dependency-free
        # fallback for trimmed portable integrations that omit the core.
        try:
            from gamemechaniclab.core.formula import FormulaEngine
            return FormulaEngine().evaluate(expression, values)
        except ImportError:
            pass
        # AST-based evaluator; imported lazily to keep the top-level module tidy.
        import ast
        import operator
        assignment = re.match(r"^\s*(?:damage|result)\s*=\s*(.+?)\s*$", expression, re.DOTALL)
        if assignment:
            expression = assignment.group(1)
        allowed_bin = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod}
        allowed_cmp = {ast.Gt: operator.gt, ast.GtE: operator.ge, ast.Lt: operator.lt, ast.LtE: operator.le, ast.Eq: operator.eq, ast.NotEq: operator.ne}
        funcs = {"max": max, "min": min, "abs": abs, "round": round}
        tree = ast.parse(expression, mode="eval")
        def visit(node: ast.AST) -> Any:
            if isinstance(node, ast.Expression): return visit(node.body)
            if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, bool)): return node.value
            if isinstance(node, ast.Name) and node.id in values: return values[node.id]
            if isinstance(node, ast.BinOp) and type(node.op) in allowed_bin:
                left, right = visit(node.left), visit(node.right)
                if isinstance(node.op, ast.Pow) and abs(float(right)) > 8: raise ValueError("Exponent too large")
                return allowed_bin[type(node.op)](left, right)
            if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd, ast.Not)):
                v = visit(node.operand); return -v if isinstance(node.op, ast.USub) else (+v if isinstance(node.op, ast.UAdd) else (not v))
            if isinstance(node, ast.Compare):
                left = visit(node.left)
                return all(allowed_cmp[type(op)](left := (left if i == 0 else visit(node.comparators[i - 1])), visit(comp)) for i, (op, comp) in enumerate(zip(node.ops, node.comparators)))
            if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
                values_ = [bool(visit(value)) for value in node.values]
                return all(values_) if isinstance(node.op, ast.And) else any(values_)
            if isinstance(node, ast.IfExp): return visit(node.body) if visit(node.test) else visit(node.orelse)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in funcs and not node.keywords:
                return funcs[node.func.id](*(visit(arg) for arg in node.args))
            raise ValueError(f"Unsupported expression node: {type(node).__name__}")
        result = visit(tree)
        if not isinstance(result, (int, float)) or not math.isfinite(float(result)):
            raise ValueError("Formula returned a non-finite value")
        return float(result)

    def _populate_formula_table(self) -> None:
        for item in self.formula_check.get_children(): self.formula_check.delete(item)
        expr = self.formula_text.get("1.0", "end-1c") if hasattr(self, "formula_text") else self.formula_expression
        for attack, mult, defense, crit in ((120, 1.0, 35, 0), (120, 1.4, 35, 0), (175, 1.7, 12, 1), (92, 0.8, 68, 0), (175, 1.7, 72, 1)):
            try:
                value = self._formula_eval(expr, {"attack": attack, "multiplier": mult, "base_damage": 0, "defense": defense, "critical_multiplier": 1.75, "resistance": 0.2, "is_critical": bool(crit)})
                out = _fmt(value)
            except Exception as exc:
                out = f"ERR: {exc}"
            self.formula_check.insert("", "end", values=(attack, mult, defense, "Yes" if crit else "No", out))

    def validate_formula(self) -> None:
        try:
            expr = self.formula_text.get("1.0", "end-1c")
            self._formula_eval(expr, {"attack": 100, "multiplier": 1, "base_damage": 0, "defense": 10, "critical_multiplier": 1.75, "resistance": 0.1, "is_critical": False})
            self.formula_expression = expr.strip()
            self.formula_valid = True
            self.formula_status.configure(text="✓ Valid and safe", foreground=GREEN)
            self._populate_formula_table()
        except Exception as exc:
            self.formula_valid = False
            self.formula_status.configure(text=f"✕ {exc}", foreground=RED)

    def apply_formula(self) -> None:
        self.validate_formula()
        if self.formula_valid:
            self.status_label.configure(text="●  CUSTOM FORMULA ACTIVE", foreground=ACCENT)
            # Formula changes are experiments themselves: immediately execute
            # the selected build/target so the effect is visible in metrics and
            # the replay without requiring a second global Run click.
            self.run_simulation()

    # ---- Replay ---------------------------------------------------------

    def _page_replay(self) -> None:
        self._page_title("Combat Replay", "Inspect every AttackStarted, Hit, Damage, Critical, Dodge, buff, and Death event.")
        body = ttk.Frame(self.content)
        body.grid(row=1, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=3)
        body.grid_columnconfigure(1, weight=2)
        body.grid_rowconfigure(0, weight=1)
        left = self._panel(body, "TIMELINE", 0, 0)
        self.replay_table = self._table(left, ("time", "type", "actor", "target", "value", "skill"), ("TIME", "EVENT", "ACTOR", "TARGET", "VALUE", "SKILL"), 17)
        events = self.last_output.get("events") or self.engine.get_replay()
        self._fill_replay(events)
        right = self._panel(body, "REPLAY VIEW", 0, 1)
        right.grid_rowconfigure(1, weight=1)
        self.replay_canvas = tk.Canvas(right, bg="#091321", highlightthickness=1, highlightbackground=GRID)
        self.replay_canvas.grid(row=1, column=0, sticky="nsew")
        self.replay_canvas.bind("<Configure>", lambda _e: self._draw_replay_scene(0))
        ttk.Label(right, text="Scrub to an event", foreground=MUTED).grid(row=2, column=0, sticky="w", pady=(10, 2))
        self.replay_scale = ttk.Scale(right, from_=0, to=max(1, len(events) - 1), orient="horizontal", command=lambda value: self._draw_replay_scene(int(float(value))))
        self.replay_scale.grid(row=3, column=0, sticky="ew")
        self._draw_replay_scene(0)

    def _fill_replay(self, events: Sequence[Mapping[str, Any]]) -> None:
        if not self._widget_alive(getattr(self, "replay_table", None)):
            return
        for iid in self.replay_table.get_children(): self.replay_table.delete(iid)
        for event in events:
            self.replay_table.insert("", "end", values=(event.get("time", ""), event.get("type", ""), event.get("actor", ""), event.get("target", ""), event.get("value", ""), event.get("skill", "")))

    def _draw_replay_scene(self, index: int) -> None:
        if not self._widget_alive(getattr(self, "replay_canvas", None)):
            return
        c = self.replay_canvas; c.delete("all")
        w, h = max(300, c.winfo_width()), max(250, c.winfo_height())
        c.create_text(14, 15, text="REPLAY FRAME", anchor="w", fill=MUTED, font=(FONT, 9, "bold"))
        c.create_oval(w * .25 - 30, h * .5 - 30, w * .25 + 30, h * .5 + 30, fill="#1f779b", outline=ACCENT, width=2)
        c.create_oval(w * .75 - 34, h * .5 - 34, w * .75 + 34, h * .5 + 34, fill="#7b2e49", outline=RED, width=2)
        events = self.last_output.get("events") or self.engine.get_replay()
        if events:
            idx = max(0, min(index, len(events) - 1)); event = events[idx]
            c.create_text(w / 2, h * .75, text=f"t = {event.get('time', 0)} s   ·   {event.get('type', 'Event')}", fill=AMBER, font=(FONT, 12, "bold"))
            c.create_text(w / 2, h * .82, text=f"{event.get('actor', '')}  →  {event.get('target', '')}  {event.get('value', '')}", fill=TEXT, font=(FONT, 9))
            if event.get("type") in ("Damage", "Critical"):
                c.create_line(w * .25 + 36, h * .5, w * .75 - 36, h * .5, fill=RED if event.get("type") == "Damage" else AMBER, width=4, arrow="last")
        else:
            c.create_text(w / 2, h * .75, text="Run a simulation to create a replay", fill=MUTED)

    # ---- execution / charts -------------------------------------------

    def _selected_build(self) -> dict[str, Any]:
        var = self.__dict__.get("arena_build_var")
        name = var.get() if var is not None else self.builds[0].get("name", "")
        return next((b for b in self.builds if b.get("name") == name), self.builds[0])

    def _selected_enemy(self) -> dict[str, Any]:
        var = self.__dict__.get("arena_enemy_var")
        name = var.get() if var is not None else self.enemies[-1].get("name", "")
        return next((e for e in self.enemies if e.get("name") == name), self.enemies[-1])

    def run_simulation(self) -> None:
        runs = _safe_int(self.runs_var.get(), 100)
        formula_widget = self.__dict__.get("formula_text")
        try:
            formula = formula_widget.get("1.0", "end-1c").strip() if formula_widget is not None else self.formula_expression
        except tk.TclError:
            formula = self.formula_expression
        try:
            # Validate at the execution boundary as well as in the Formula
            # page.  A user can edit the text and press the global Run button
            # without visiting Validate first; invalid input must not silently
            # fall back to the baseline damage model.
            from gamemechaniclab.core.formula import FormulaEngine
            formula = FormulaEngine().compile(formula).expression
        except Exception as exc:
            self.formula_valid = False
            if self._widget_alive(getattr(self, "formula_status", None)):
                self.formula_status.configure(text=f"✕ {exc}", foreground=RED)
            self.status_label.configure(text=f"●  FORMULA REJECTED  ·  {exc}", foreground=RED)
            return
        config = {"build": self._selected_build(), "enemy": self._selected_enemy(), "seed": self.experiment.get("seed", 42), "skills": self.skills, "enemy_skills": self.enemy_skills, "formula": formula, "simulation": dict(self.simulation_defaults)}
        self.status_label.configure(text=f"●  RUNNING {runs:,} FIGHTS…", foreground=AMBER)
        self.update_idletasks()
        output = self.engine.run_fights(config, runs, capture=True)
        self.last_output = output
        self._record_experiment_run(config, output, runs)
        self._set_arena_replay(output.get("events", []), reset=True)
        self.status_label.configure(text=f"●  SIMULATION COMPLETE  ·  {runs:,} FIGHTS", foreground=GREEN)
        self._update_stats(output.get("summary", {}))
        self._update_warnings(config, output)
        if self._widget_alive(getattr(self, "arena_feed", None)):
            self.arena_feed.delete("1.0", "end")
            for event in output.get("events", [])[-20:]:
                self._write_feed(self._event_line(event), AMBER if event.get("type") in ("Critical", "Death") else TEXT)
            if not output.get("events"):
                self._write_feed("No replay events returned by the engine. Use a capture-enabled core adapter.", MUTED)
            self._draw_arena(output)
        if self.current_page == "Replay" and self._widget_alive(getattr(self, "replay_canvas", None)):
            self._fill_replay(output.get("events", [])); self._draw_replay_scene(0)

    def _update_warnings(self, config: Mapping[str, Any], output: Mapping[str, Any]) -> None:
        if not self._widget_alive(getattr(self, "warning_text", None)):
            return
        rows = self.engine.detect_warnings(config, output)
        self.warning_text.configure(state="normal")
        self.warning_text.delete("1.0", "end")
        if not rows:
            self.warning_text.insert("1.0", "✓ No pathological patterns detected in this run.")
        else:
            for row in rows:
                severity = str(row.get("severity", "medium")).upper()
                self.warning_text.insert("end", f"{severity:<6}  {row.get('type', row.get('code', 'Warning'))}  ·  {row.get('message', '')}\n")
        self.warning_text.configure(state="disabled")

    @staticmethod
    def _event_line(event: Mapping[str, Any]) -> str:
        bits = [f"{_safe_float(event.get('time', 0)):>6.2f}s", f"{str(event.get('type', 'Event')):<12}", str(event.get("actor", ""))]
        if event.get("target"): bits.append("→ " + str(event["target"]))
        if event.get("skill"): bits.append("[" + str(event["skill"]) + "]")
        if event.get("value") is not None: bits.append(str(event["value"]))
        return "  ".join(bits)

    def _update_stats(self, summary: Mapping[str, Any]) -> None:
        if self._widget_alive(getattr(self, "stat_ttk", None)):
            self.stat_ttk.set(f"{_safe_float(summary.get('win_rate')) * 100:.1f}%")
            self.stat_dps.set(f"{_safe_float(summary.get('avg_dps')):.1f}")
            self.stat_ttk2.set(f"{_safe_float(summary.get('avg_ttk')):.1f}s")
            self.stat_taken.set(f"{_safe_float(summary.get('avg_damage_taken')):.1f}")

    def run_sweep(self) -> None:
        formula_widget = self.__dict__.get("formula_text")
        try:
            formula = formula_widget.get("1.0", "end-1c").strip() if formula_widget is not None else self.formula_expression
        except tk.TclError:
            formula = self.formula_expression
        try:
            from gamemechaniclab.core.formula import FormulaEngine
            formula = FormulaEngine().compile(formula).expression
        except Exception as exc:
            self.formula_valid = False
            self.status_label.configure(text=f"●  SWEEP REJECTED  ·  {exc}", foreground=RED)
            return
        self.status_label.configure(text="●  RUNNING PARAMETER SWEEP…", foreground=AMBER)
        self.update_idletasks()
        sweep_config = {"builds": self.builds, "enemy": self.enemies[-1], "skills": self.skills, "enemy_skills": self.enemy_skills, "boss_hp_values": [5000, 7500, 10000, 12500], "formula": formula, "simulation": dict(self.simulation_defaults)}
        output = self.engine.run_sweep(sweep_config, _safe_int(self.runs_var.get(), 100))
        self.last_output = {"sweep": output}
        self._record_experiment_run(sweep_config, output, _safe_int(self.runs_var.get(), 100))
        self.status_label.configure(text="●  SWEEP COMPLETE  ·  3 BUILDS × 4 HP VALUES", foreground=GREEN)
        self._show_sweep_window(output)

    def _show_sweep_window(self, output: Mapping[str, Any]) -> None:
        win = tk.Toplevel(self)
        win.title("Parameter Sweep  ·  Balance Heatmap")
        win.geometry("980x650")
        win.configure(bg=BG)
        win.transient(self)
        ttk.Label(win, text="PARAMETER SWEEP", style="Title.TLabel").pack(anchor="w", padx=20, pady=(18, 0))
        ttk.Label(win, text="Win rate across builds and boss HP values", style="Subtitle.TLabel").pack(anchor="w", padx=20, pady=(2, 12))
        holder = ttk.Frame(win, style="Panel.TFrame", padding=12)
        holder.pack(fill="both", expand=True, padx=15, pady=5)
        holder.grid_columnconfigure(0, weight=1); holder.grid_columnconfigure(1, weight=2); holder.grid_rowconfigure(0, weight=1)
        table = self._table(holder, ("build", "boss_hp", "win_rate", "avg_ttk", "avg_dps", "deaths"), ("BUILD", "BOSS HP", "WIN RATE", "AVG TTK", "AVG DPS", "DEATHS"), 14)
        table.pack_forget(); table.pack(side="left", fill="both", expand=True, padx=(0, 12))
        for cell in output.get("cells", []):
            table.insert("", "end", values=(cell.get("build"), cell.get("boss_hp"), f"{_safe_float(cell.get('win_rate'))*100:.1f}%", f"{_safe_float(cell.get('avg_ttk')):.1f}s", f"{_safe_float(cell.get('avg_dps')):.1f}", cell.get("deaths")))
        chart = tk.Canvas(holder, width=450, height=440, bg="#0a1525", highlightthickness=1, highlightbackground=GRID)
        chart.pack(side="right", fill="both", expand=True)
        self._draw_heatmap(chart, output)
        ttk.Button(win, text="Close", style="Ghost.TButton", command=win.destroy).pack(anchor="e", padx=20, pady=12)

    def _draw_heatmap(self, c: tk.Canvas, output: Mapping[str, Any]) -> None:
        c.delete("all")
        cells = output.get("cells", []); hps = list(output.get("boss_hp_values", [])); names = list(output.get("build_names", []))
        if not hps or not names: return
        w, h = max(350, c.winfo_width()), max(300, c.winfo_height()); left, top = 120, 45; cw = max(46, (w - left - 20) / len(hps)); ch = max(42, (h - top - 30) / len(names))
        c.create_text(left, 18, text="WIN RATE HEATMAP", anchor="w", fill=TEXT, font=(FONT, 11, "bold"))
        for j, hp in enumerate(hps): c.create_text(left + j*cw + cw/2, top - 14, text=f"{hp/1000:.1f}k", fill=MUTED, font=(FONT, 8))
        by_key = {(str(x.get("build")), int(x.get("boss_hp"))): x for x in cells}
        for i, name in enumerate(names):
            c.create_text(left - 8, top + i*ch + ch/2, text=name[:15], anchor="e", fill=MUTED, font=(FONT, 8))
            for j, hp in enumerate(hps):
                cell = by_key.get((str(name), int(hp)), {}); rate = _safe_float(cell.get("win_rate"))
                # blue (low) → teal/green (high)
                color = f"#{int(30 + 45*rate):02x}{int(60 + 170*rate):02x}{int(100 + 120*rate):02x}"
                x, y = left + j*cw, top + i*ch
                c.create_rectangle(x+2, y+2, x+cw-2, y+ch-2, fill=color, outline=GRID)
                c.create_text(x+cw/2, y+ch/2, text=f"{rate*100:.0f}%", fill="white", font=(FONT, 9, "bold"))

    # ---- persistence ----------------------------------------------------

    def _record_experiment_run(self, config: Mapping[str, Any], output: Mapping[str, Any], runs: int) -> None:
        """Fold the latest real run into the portable experiment record."""
        summary = output.get("summary", output) if isinstance(output, Mapping) else {}
        self.experiment.update({
            "runs": max(1, int(runs)),
            "config": {
                "builds": self.builds,
                "enemies": self.enemies,
                "skills": self.skills,
                "enemy_skills": self.enemy_skills,
                "simulation": dict(config.get("simulation", self.simulation_defaults)) if isinstance(config, Mapping) else dict(self.simulation_defaults),
                "formula": config.get("formula", self.formula_expression) if isinstance(config, Mapping) else self.formula_expression,
            },
            "results": {
                "summary": dict(summary) if isinstance(summary, Mapping) else {},
                "sweep": output.get("cells", []) if isinstance(output, Mapping) else [],
                "event_count": len(output.get("events", []) or []) if isinstance(output, Mapping) else 0,
                "fight_results": output.get("results", []) if isinstance(output, Mapping) else [],
            },
            "status": "completed",
        })
        if self._widget_alive(getattr(self, "exp_status", None)):
            self.exp_status.configure(text="Completed", foreground=GREEN)

    def _experiment_record(self) -> dict[str, Any]:
        """Return the canonical Hypothesis/Config/Runs/Results/Conclusion shape."""
        record = dict(self.experiment)
        record.setdefault("id", "ui_experiment_001")
        record.setdefault("name", "Game Mechanic Lab experiment")
        record.setdefault("hypothesis", "")
        record.setdefault("config", {})
        record.setdefault("runs", 1)
        record.setdefault("results", {})
        record.setdefault("conclusion", "")
        record.setdefault("status", "draft")
        return record

    def _payload(self) -> dict[str, Any]:
        for item in self.builds + self.enemies:
            self._sync_editor_stats(item, tuple(item.keys()))
        record = self._experiment_record()
        return {"project": "Game Mechanic Lab", "version": "0.1.0", "experiment": record, "record": record, "mechanics": self.mechanics_catalog, "skills": self.skills, "enemy_skills": self.enemy_skills, "builds": self.builds, "enemies": self.enemies, "last_output": self.last_output}

    def export_experiment(self) -> None:
        path = filedialog.asksaveasfilename(title="Save experiment", defaultextension=".json", filetypes=(("JSON", "*.json"), ("YAML-like text", "*.yaml"), ("All files", "*.*")))
        if not path: return
        try:
            Path(path).write_text(_json_text(self._payload()), encoding="utf-8")
            self.status_label.configure(text=f"●  SAVED  ·  {Path(path).name}", foreground=GREEN)
        except OSError as exc:
            messagebox.showerror("Save failed", str(exc))

    def import_config(self) -> None:
        path = filedialog.askopenfilename(title="Import experiment", filetypes=(("JSON / YAML", "*.json *.yaml *.yml"), ("All files", "*.*")))
        if not path: return
        try:
            payload = _read_json_or_yaml(Path(path).read_text(encoding="utf-8"))
            raw_experiment = payload.get("experiment") if isinstance(payload.get("experiment"), Mapping) else payload
            if isinstance(raw_experiment, Mapping):
                self.experiment.update(raw_experiment)
                raw_config = raw_experiment.get("config", {})
                if isinstance(raw_config, Mapping) and raw_config.get("formula"):
                    self.formula_expression = str(raw_config["formula"])
                if isinstance(raw_experiment.get("results"), Mapping):
                    self.last_output = dict(raw_experiment["results"])
            if isinstance(payload.get("last_output"), Mapping):
                # Preserve the full replay stream when restoring an exported
                # UI payload; ExperimentRecord.results intentionally stays
                # compact for reports.
                self.last_output = dict(payload["last_output"])
            # Resolve compact skill/status references before the editor stores
            # them.  This mirrors ``load_project`` and keeps imported phase
            # add_status/skill IDs from becoming empty effects.
            raw_config = raw_experiment.get("config", {}) if isinstance(raw_experiment, Mapping) else {}
            config_skills = raw_config.get("skills") if isinstance(raw_config, Mapping) else None
            config_builds = raw_config.get("builds") if isinstance(raw_config, Mapping) else None
            config_enemies = raw_config.get("enemies") if isinstance(raw_config, Mapping) else None
            catalogs = {
                "mechanics": payload.get("mechanics", self.mechanics_catalog),
                "skills": {"skills": payload["skills"] if isinstance(payload.get("skills"), list) else (config_skills if isinstance(config_skills, list) else [])},
                "builds": {"builds": payload["builds"] if isinstance(payload.get("builds"), list) else (config_builds if isinstance(config_builds, list) else [])},
                "enemies": {"enemies": payload["enemies"] if isinstance(payload.get("enemies"), list) else (config_enemies if isinstance(config_enemies, list) else [])},
                "bosses": {"bosses": payload.get("bosses", []) if isinstance(payload.get("bosses"), list) else []},
            }
            try:
                from gamemechaniclab.core.data import resolve_catalogue_references
                resolve_catalogue_references(catalogs)
            except (ImportError, TypeError, ValueError):
                pass
            resolved_skills = catalogs["skills"].get("skills", [])
            resolved_builds = catalogs["builds"].get("builds", [])
            resolved_enemies = catalogs["enemies"].get("enemies", [])
            if isinstance(payload.get("skills"), list) or isinstance(config_skills, list):
                self.skills[:] = [dict(x) for x in resolved_skills if isinstance(x, Mapping)]
            if isinstance(payload.get("enemy_skills"), list): self.enemy_skills[:] = payload["enemy_skills"]
            if isinstance(payload.get("builds"), list) or isinstance(config_builds, list):
                self.builds[:] = [dict(x) for x in resolved_builds if isinstance(x, Mapping)]
            if isinstance(payload.get("enemies"), list) or isinstance(config_enemies, list):
                self.enemies[:] = [dict(x) for x in resolved_enemies if isinstance(x, Mapping)]
            self._flatten_editor_stats()
            for build in self.builds:
                build.setdefault("bot", "optimal")
            self.status_label.configure(text=f"●  IMPORTED  ·  {Path(path).name}", foreground=GREEN)
            self.show_page(self.current_page)
        except Exception as exc:
            messagebox.showerror("Import failed", str(exc))


def launch() -> None:
    """Launch the desktop application."""
    app = GameMechanicLabApp()
    app.mainloop()


# Short aliases are convenient for small integrations and keep the UI boundary
# friendly to launchers that conventionally import ``App``.
App = GameMechanicLabApp


if __name__ == "__main__":  # pragma: no cover
    launch()
