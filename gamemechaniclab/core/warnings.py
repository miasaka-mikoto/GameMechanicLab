"""Static and empirical balance diagnostics.

Warnings are deliberately explainable heuristics.  They are signals for a
designer to inspect, not claims that a build is universally broken.  Every
record includes a stable code, severity, message, and evidence payload so it
can be shown in the UI or stored with an experiment.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any, Iterable, Mapping, Sequence

from .formula import FormulaEngine, FormulaError
from .models import Build, Enemy, Skill


@dataclass
class BalanceWarning:
    code: str
    severity: str
    message: str
    evidence: dict[str, Any]

    @property
    def type(self) -> str:
        return self.code

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["type"] = self.code
        return d


def _record(code: str, severity: str, message: str, **evidence: Any) -> BalanceWarning:
    return BalanceWarning(code, severity, message, evidence)


def _skills(value: Any) -> list[Skill]:
    if isinstance(value, Build):
        return list(value.skills)
    if isinstance(value, Enemy):
        return list(value.skills)
    if isinstance(value, Mapping):
        raw = value.get("skills", []) or []
        return [x if isinstance(x, Skill) else Skill.from_dict(x) for x in raw]
    return []


def _extract_stats(value: Any) -> Mapping[str, Any]:
    if isinstance(value, (Build, Enemy)):
        return value.stats.to_dict()
    if isinstance(value, Mapping):
        stats = value.get("stats")
        return stats if isinstance(stats, Mapping) else value
    return {}


def _batch_mapping(result: Any) -> Mapping[str, Any]:
    if result is None:
        return {}
    if isinstance(result, Mapping):
        return result
    if hasattr(result, "to_dict"):
        try:
            return result.to_dict(include_fights=True)
        except TypeError:
            return result.to_dict()
    return getattr(result, "__dict__", {})


def detect_balance_warnings(
    player: Build | Mapping[str, Any] | None = None,
    enemy: Enemy | Mapping[str, Any] | None = None,
    result: Any = None,
    batch: Any = None,
    formula: str | None = None,
    thresholds: Mapping[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Return explainable diagnostics for pathological mechanics.

    For convenience the first positional argument may also be a batch result
    mapping; in that case pass the player/enemy by keyword or omit them.
    """
    if result is None and batch is not None:
        result = batch
    if result is None and isinstance(player, Mapping) and any(k in player for k in ("win_rate", "average_dps", "summary", "fights")):
        result, player = player, None
    thresholds = dict(thresholds or {})
    warnings: list[BalanceWarning] = []
    pstats, estats = _extract_stats(player), _extract_stats(enemy)
    pskills, eskills = _skills(player), _skills(enemy)

    crit = float(pstats.get("critical", pstats.get("crit", 0)) or 0)
    if crit > 1.0:
        crit = crit / 100.0
    if crit >= 0.999:
        warnings.append(_record("100% Crit", "high", "Critical chance reaches 100%; every eligible hit is critical.", critical_chance=crit))

    all_skills = pskills + eskills
    zero_cd = [s.id for s in all_skills if float(s.cooldown) <= 0.0]
    cdr = float(pstats.get("cooldown_reduction", 0) or 0)
    if cdr > 1:
        cdr /= 100.0
    if zero_cd or cdr >= 0.95:
        warnings.append(_record("Cooldown Loop", "high", "A skill can be recast without a meaningful cooldown window.", zero_cooldown_skills=zero_cd, cooldown_reduction=cdr))

    stun_skills = [s for s in all_skills if float(s.hitstun) > 0]
    if stun_skills:
        max_stun = max(float(s.hitstun) for s in stun_skills)
        # A single hitstun longer than the shortest cooldown is a strong
        # static indicator; empirical traces below strengthen the signal.
        min_cd = min(max(0.001, float(s.cooldown)) for s in stun_skills)
        if max_stun >= min_cd * 0.9:
            warnings.append(_record("Permanent Stun", "high", "Hitstun duration is long enough to chain before recovery.", max_hitstun=max_stun, shortest_cooldown=min_cd))

    combo_skills = [s for s in pskills if s.combo_step or "combo" in [str(t).lower() for t in s.tags]]
    if combo_skills or len(pskills) >= 6:
        warnings.append(_record("Infinite Combo", "medium", "The rotation exposes a repeating combo path; inspect resource and recovery limits.", combo_skills=[s.id for s in combo_skills], skill_count=len(pskills)))

    # Formula probe catches explicitly negative damage before a run can hide it
    # behind max(0, ...).  Do not flag ordinary zero-damage utility skills.
    expression = formula or (getattr(player, "formula", None) if player is not None else None)
    if expression:
        engine = FormulaEngine()
        try:
            probe = engine.evaluate(expression, {"attack": float(pstats.get("attack", 0) or 0), "defense": float(estats.get("defense", 0) or 0), "multiplier": 1.0, "base_damage": 0.0, "critical_multiplier": 1.5})
            if probe < 0:
                warnings.append(_record("Negative Damage", "high", "The configured formula produces negative damage for the probe values.", probe=probe, formula=expression))
        except FormulaError as exc:
            warnings.append(_record("Negative Damage", "medium", "The configured formula could not be evaluated safely.", formula=expression, error=str(exc)))
    elif float(pstats.get("attack", 0) or 0) < 0 or any(float(s.damage) < 0 for s in all_skills):
        warnings.append(_record("Negative Damage", "high", "Attack or a skill base damage is negative.", attack=pstats.get("attack"), skills=[s.id for s in all_skills if s.damage < 0]))

    mapping = _batch_mapping(result)
    summary = mapping.get("summary", mapping)
    avg_dps = summary.get("average_dps", summary.get("avg_dps", summary.get("dps", 0))) or 0
    try:
        avg_dps = float(avg_dps)
    except (TypeError, ValueError):
        avg_dps = 0.0
    attack = max(1.0, float(pstats.get("attack", 0) or 0))
    explosion_ratio = float(thresholds.get("dps_ratio", 8.0))
    if avg_dps > attack * explosion_ratio or avg_dps > float(thresholds.get("absolute_dps", 10000.0)):
        warnings.append(_record("DPS Explosion", "high", "Observed DPS is an extreme multiple of attack power.", average_dps=avg_dps, attack=attack, ratio=avg_dps / attack))

    fights = mapping.get("fights", []) or []
    if fights:
        if all(not bool(f.get("win")) for f in fights if isinstance(f, Mapping)):
            warnings.append(_record("Unkillable Build", "medium", "The player never defeats the target in the supplied sample.", runs=len(fights), win_rate=0.0))
        combo_counts = []
        for fight in fights:
            events = fight.get("events", []) if isinstance(fight, Mapping) else []
            combo_counts.append(sum(1 for e in events if str(e.get("type", "")) == "ComboStep"))
        if combo_counts and max(combo_counts) >= int(thresholds.get("combo_steps", 100)):
            warnings.append(_record("Infinite Combo", "high", "A replay contains an unusually long uninterrupted combo chain.", max_combo_steps=max(combo_counts)))
    elif summary:
        win_rate = summary.get("win_rate")
        if win_rate is not None and float(win_rate) <= 0 and enemy is not None:
            warnings.append(_record("Unkillable Build", "medium", "The player has zero wins in the supplied aggregate.", win_rate=float(win_rate)))

    # Unique codes make the warning panel and machine-readable reports stable.
    result_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for warning in warnings:
        if warning.code not in seen:
            result_rows.append(warning.to_dict()); seen.add(warning.code)
    return result_rows


__all__ = ["BalanceWarning", "detect_balance_warnings"]

