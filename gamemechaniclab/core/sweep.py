"""Cartesian parameter sweeps and heatmap projections."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
import copy
import itertools
import math
from typing import Any, Iterable, Mapping, MutableMapping, Sequence

from .models import Build, Enemy, SimulationConfig
from .simulation import CombatSimulator


def _values(spec: Any) -> list[Any]:
    """Expand a sweep axis specification into concrete values."""
    if isinstance(spec, Mapping):
        if "values" in spec:
            return list(spec["values"])
        start, stop = spec.get("start"), spec.get("stop")
        step = spec.get("step", 1)
        if start is not None and stop is not None:
            start, stop, step = float(start), float(stop), float(step)
            if step == 0:
                raise ValueError("sweep step cannot be zero")
            out: list[Any] = []
            value = start
            forward = step > 0
            while (value <= stop + 1e-12) if forward else (value >= stop - 1e-12):
                out.append(int(value) if value.is_integer() else value)
                value += step
            return out
    if isinstance(spec, range):
        return list(spec)
    if isinstance(spec, (list, tuple, set)):
        return list(spec)
    return [spec]


def _as_plain(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    return value


def _set_path(root: Any, path: str, value: Any) -> None:
    """Set a dotted path on a dataclass or mapping, with friendly aliases."""
    path = str(path).strip()
    aliases = {
        "player.": "player.", "build.": "player.", "boss.": "enemy.",
        "boss_hp": "enemy.stats.hp", "enemy_hp": "enemy.stats.hp",
        "player_attack": "player.stats.attack", "boss_defense": "enemy.stats.defense",
    }
    if path in aliases:
        path = aliases[path]
    parts = [p for p in path.split(".") if p]
    if parts and parts[0] in {"player", "build"}:
        parts[0] = "_player"
    elif parts and parts[0] in {"enemy", "boss", "target"}:
        parts[0] = "_enemy"

    def set_one(obj: Any, name: str, val: Any) -> None:
        if isinstance(obj, MutableMapping):
            obj[name] = val
        else:
            setattr(obj, name, val)

    # The caller gives us a small holder mapping; resolve aliases first.
    if not parts:
        return
    current: Any = root
    for idx, part in enumerate(parts[:-1]):
        if isinstance(current, MutableMapping):
            if part not in current:
                current[part] = {}
            current = current[part]
        else:
            current = getattr(current, part)
    final = parts[-1]
    if isinstance(current, MutableMapping):
        current[final] = value
    else:
        setattr(current, final, value)


def _set_parameter(player: Any, enemy: Any, path: str, value: Any) -> None:
    """Set a parameter using the public axis names used by the UI/tests."""
    path = str(path)
    # Accept the fully-qualified names used by the bundled experiment file.
    # The sweep API still operates on one selected player/enemy pair per cell.
    if path.startswith("bosses."):
        pieces = path.split(".")
        path = ".".join(pieces[2:]) if len(pieces) > 2 else path
        path = "enemy." + path
    elif path in {"player_i_frame", "scenario.player_i_frame", "player.iframe", "scenario.player.iframe"}:
        path = "player.stats.iframe"
    elif path in {"damage_multiplier", "formulas.damage_multiplier"}:
        # A formula multiplier axis is represented as a multiplier on every
        # currently equipped skill; this keeps the damage rule data-driven
        # without executing or rewriting formula text.
        for skill in getattr(player, "skills", []) or []:
            skill.multiplier *= float(value)
        return
    elif path in {"scenario.player_build", "player_build"}:
        # Build selection is handled by the caller when it sweeps a catalogue;
        # a string axis is metadata when a single Build object is supplied.
        return
    if path in {"boss_hp", "enemy_hp", "enemy.hp", "boss.hp"}:
        path = "enemy.stats.hp"
    if path in {"player_attack", "build_attack", "player.attack", "build.attack"}:
        path = "player.stats.attack"
    target = player if path.startswith(("player.", "build.")) else enemy if path.startswith(("enemy.", "boss.", "target.")) else None
    if target is None:
        # Unqualified paths default to player stats when a matching field exists.
        target = player
    parts = path.split(".")
    if parts[0] in {"player", "build", "enemy", "boss", "target"}:
        parts = parts[1:]
    if parts and parts[0] == "stats":
        parts = parts[1:]
        holder = target.stats if hasattr(target, "stats") else target
    else:
        holder = target
    if not parts:
        return
    current = holder
    for part in parts[:-1]:
        if isinstance(current, MutableMapping):
            current = current.setdefault(part, {})
        else:
            current = getattr(current, part)
    final = parts[-1]
    if isinstance(current, MutableMapping):
        current[final] = value
    else:
        setattr(current, final, value)
    # Dataclass Stats normalises probability/limits in __post_init__; rerun it
    # after a direct sweep mutation.
    if hasattr(target, "stats") and hasattr(target.stats, "__post_init__"):
        target.stats.__post_init__()
        # HP axes describe the combatant's starting/maximum health.  Keeping
        # an old max_hp from the source catalogue makes phase thresholds and
        # conditional passives use the wrong denominator in every sweep cell.
        # An explicit max_hp axis can still override this when it is applied
        # after the hp axis in the declared Cartesian parameter order.
        if final == "hp" and hasattr(target.stats, "max_hp"):
            target.stats.max_hp = float(target.stats.hp)


def _set_config_parameter(config: SimulationConfig, path: str, value: Any) -> bool:
    """Apply an axis that belongs to the simulation config rather than an actor.

    Formula text is a first-class sweep dimension.  Keeping it on the
    per-cell :class:`SimulationConfig` means a formula comparison uses the
    same safe parser and event-driven simulator as an ordinary fight; it does
    not rewrite Python source or mutate the caller's config.
    """
    normalized = str(path).strip().lower()
    if normalized in {"formula", "formula_text", "simulation.formula", "config.formula", "formulas.formula"}:
        config.formula = str(value)
        return True
    return False


def parameter_sweep(
    player: Build | Mapping[str, Any],
    enemy: Enemy | Mapping[str, Any],
    parameters: Mapping[str, Any] | None = None,
    runs: int = 100,
    seed: int | None = None,
    config: SimulationConfig | Mapping[str, Any] | None = None,
    sweep: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Run every Cartesian combination of ``parameters``.

    Each returned row retains the axis values in both ``parameters`` and
    flattened dotted-key fields, alongside the normal combat metrics.  Inputs
    are deep-copied for every cell, so a sweep never mutates an editor build.
    """
    axes = dict(parameters or sweep or {})
    names = list(axes)
    choices = [_values(axes[name]) for name in names]
    if not names:
        choices = [()]  # one baseline cell
    base_config = config if isinstance(config, SimulationConfig) else SimulationConfig.from_dict(config or {})
    base_config = copy.deepcopy(base_config)
    simulator = CombatSimulator(base_config)
    rows: list[dict[str, Any]] = []
    for index, combination in enumerate(itertools.product(*choices)):
        p = copy.deepcopy(player)
        e = copy.deepcopy(enemy)
        # Mapping inputs are converted once so path mutation is predictable.
        if not isinstance(p, Build):
            p = Build.from_dict(p)
        if not isinstance(e, Enemy):
            e = Enemy.from_dict(e)
        values = dict(zip(names, combination))
        cell_config = copy.deepcopy(base_config)
        for path, value in values.items():
            if not _set_config_parameter(cell_config, path, value):
                _set_parameter(p, e, path, value)
        run_seed = (int(seed) + index * 100003) if seed is not None else None
        result = simulator.run_batch(p, e, runs=max(1, int(runs)), config=cell_config, seed=run_seed, keep_fights=False)
        row = {
            "index": index,
            "parameters": values,
            "runs": result.runs,
            "wins": result.wins,
            "losses": result.losses,
            "win_rate": result.win_rate,
            "average_ttk": result.average_ttk,
            "median_ttk": result.median_ttk,
            "avg_ttk": result.average_ttk,
            "average_dps": result.average_dps,
            "avg_dps": result.average_dps,
            "average_damage_taken": result.average_damage_taken,
            "damage_taken": result.average_damage_taken,
            "deaths": result.deaths,
            "player_deaths": result.player_deaths,
            "skill_usage": result.skill_usage,
            "seed": run_seed,
        }
        row.update(values)
        # Preserve the effective formula explicitly.  Without this field a
        # formula axis is only recoverable from the dotted input key, which is
        # inconvenient for CSV/report consumers and heatmap annotations.  Set
        # it after flattening so assignment syntax is reported normalized in
        # the same way as FormulaEngine.
        row["formula"] = cell_config.formula
        rows.append(row)
    return rows


def project_heatmap(rows: Sequence[Mapping[str, Any]], x: str, y: str, metric: str = "win_rate") -> dict[str, Any]:
    """Project sweep rows to a matrix suitable for a heatmap widget."""
    def get(row: Mapping[str, Any], key: str) -> Any:
        if key in row:
            return row[key]
        params = row.get("parameters", {})
        if isinstance(params, Mapping) and key in params:
            return params[key]
        return None

    # Keep numeric axes in numeric order while remaining stable for mixed or
    # partially missing editor values.
    def axis_key(value: Any) -> tuple[str, Any]:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return ("0", float(value))
        return ("1", "" if value is None else str(value))
    xs = sorted({get(r, x) for r in rows}, key=axis_key)
    ys = sorted({get(r, y) for r in rows}, key=axis_key)
    lookup = {(get(r, x), get(r, y)): r for r in rows}
    matrix: list[list[float | None]] = []
    for y_value in ys:
        line: list[float | None] = []
        for x_value in xs:
            value = lookup.get((x_value, y_value), {}).get(metric)
            try:
                line.append(float(value) if value is not None else None)
            except (TypeError, ValueError):
                line.append(None)
        matrix.append(line)
    return {"x": x, "y": y, "metric": metric, "x_values": xs, "y_values": ys, "matrix": matrix}


def heatmap(rows: Sequence[Mapping[str, Any]], x: str, y: str, metric: str = "win_rate") -> dict[str, Any]:
    return project_heatmap(rows, x, y, metric)


__all__ = ["parameter_sweep", "project_heatmap", "heatmap"]
