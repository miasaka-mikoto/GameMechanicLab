"""Headless experiment runner for Game Mechanic Lab.

This is intentionally part of the product, not a test helper: it runs the
same event-driven core as the desktop Arena and writes reproducible artifacts
for a real balance study.
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

from .core import (
    Build,
    Enemy,
    ExperimentRecord,
    FormulaEngine,
    SimulationConfig,
    compare_formula_variants,
    detect_balance_warnings,
    parameter_sweep,
    project_heatmap,
    render_report,
    run_batch,
    save_experiment,
)
from .core.data import load_project


ROOT = Path(__file__).resolve().parents[1]


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_demo_catalog(root: Path = ROOT) -> dict[str, Any]:
    """Load the bundled catalogue through the same resolver used by editors.

    Keeping the CLI on the public data boundary is important: status IDs,
    phase ``add_status`` records, and compact skill references must behave the
    same way in a desktop Arena, a headless experiment, and a caller using
    :func:`load_project` directly.
    """
    project = load_project(root)
    skills_doc = project.get("skills", {}) if isinstance(project.get("skills"), Mapping) else {}
    builds_doc = project.get("builds", {}) if isinstance(project.get("builds"), Mapping) else {}
    enemies_doc = project.get("enemies", {}) if isinstance(project.get("enemies"), Mapping) else {}
    bosses_doc = project.get("bosses", {}) if isinstance(project.get("bosses"), Mapping) else {}

    def expand(raw: Mapping[str, Any], skill_lookup: Mapping[str, Any]) -> dict[str, Any]:
        item = copy.deepcopy(dict(raw))
        item["skills"] = [
            copy.deepcopy(skill_lookup.get(str(s), {"id": str(s)})) if isinstance(s, str) else copy.deepcopy(s)
            for s in raw.get("skills", [])
        ]
        return item

    skill_map = {
        str(item.get("id")): item
        for item in skills_doc.get("skills", [])
        if isinstance(item, Mapping) and item.get("id") is not None
    }
    enemy_skill_map: dict[str, Any] = {}
    for doc, key in ((enemies_doc, "enemy_skills"), (bosses_doc, "boss_skills")):
        values = doc.get(key, {}) if isinstance(doc, Mapping) else {}
        if isinstance(values, Mapping):
            enemy_skill_map.update({str(k): v for k, v in values.items()})

    builds = [Build.from_dict(expand(raw, skill_map)) for raw in builds_doc.get("builds", [])]
    enemies = [Enemy.from_dict(expand(raw, enemy_skill_map)) for raw in enemies_doc.get("enemies", [])]
    bosses = [Enemy.from_dict(expand(raw, enemy_skill_map)) for raw in bosses_doc.get("bosses", [])]
    mechanics = project.get("mechanics", {}) if isinstance(project.get("mechanics"), Mapping) else {}
    formula_defaults = mechanics.get("formulas", {}) if isinstance(mechanics.get("formulas"), Mapping) else {}
    return {
        "skills": list(skill_map.values()),
        "builds": builds,
        "enemies": enemies,
        "bosses": bosses,
        "simulation_defaults": copy.deepcopy(mechanics.get("simulation_defaults", {})),
        "formula_defaults": copy.deepcopy(formula_defaults),
    }


def _batch_payload(batch: Any, include_fights: bool = True) -> dict[str, Any]:
    payload = batch.to_dict(include_fights=include_fights) if hasattr(batch, "to_dict") else dict(batch)
    payload["avg_ttk"] = payload.get("average_ttk")
    payload["avg_dps"] = payload.get("average_dps")
    return payload


def _validated_formula(expression: str | None) -> str:
    """Validate and normalize editor/CLI formula text before any runs start."""
    candidate = str(expression or SimulationConfig().formula)
    return FormulaEngine().compile(candidate).expression


def _catalogue_simulation_config(
    catalogue: Mapping[str, Any],
    *,
    seed: int,
    max_time: float,
    record_replay: bool,
    formula: str,
) -> SimulationConfig:
    """Build a simulation config from the editable mechanics defaults.

    Experiment-level values (seed, time budget, replay capture, and the
    selected formula) deliberately override the catalogue defaults.  Action
    tuning such as attack range, dodge cost, and aggregate exposure therefore
    remains editable in JSON/YAML instead of being duplicated in the CLI.
    """
    defaults = catalogue.get("simulation_defaults", {})
    raw = dict(defaults) if isinstance(defaults, Mapping) else {}
    raw.update({"seed": seed, "max_time": max_time, "record_replay": record_replay, "formula": formula})
    return SimulationConfig.from_dict(raw)


def _parse_formula_variants(values: list[str] | None) -> dict[str, str]:
    """Parse repeatable ``NAME=EXPRESSION`` CLI arguments.

    Splitting only at the first equals sign permits formulas containing
    comparisons such as ``target_hp > 0``.  The safe FormulaEngine performs
    the actual syntax validation before any simulation begins.
    """
    variants: dict[str, str] = {}
    for raw in values or []:
        label, separator, expression = str(raw).partition("=")
        label, expression = label.strip(), expression.strip()
        if not separator or not label or not expression:
            raise ValueError(f"formula variant must use NAME=EXPRESSION: {raw!r}")
        if label in variants:
            raise ValueError(f"duplicate formula variant label: {label}")
        variants[label] = expression
    return variants


def _write_formula_comparison_artifacts(output: Path, comparison: Mapping[str, Any]) -> None:
    """Write a compact JSON + CSV view of Formula Lab comparison results."""
    (output / "formula_comparison.json").write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    rows = list(comparison.get("formula_rows", []))
    if not rows:
        return
    fields = [
        "index", "formula_id", "formula", "runs", "win_rate", "average_ttk",
        "median_ttk", "average_dps", "average_damage_taken", "deaths", "seed",
        "delta_win_rate", "delta_average_ttk", "delta_median_ttk",
        "delta_average_dps", "delta_average_damage_taken", "delta_deaths",
    ]
    with (output / "formula_comparison.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            item = dict(row)
            delta = item.pop("delta_vs_baseline", {}) or {}
            item.update({f"delta_{key}": value for key, value in delta.items()})
            writer.writerow(item)


def _write_svg_heatmap(output: Path, heatmap: Mapping[str, Any], title: str, filename: str = "win_rate_heatmap.svg", percent: bool = True) -> Path:
    xs, ys, matrix = heatmap.get("x_values", []), heatmap.get("y_values", []), heatmap.get("matrix", [])
    width, cell_w, cell_h, left, top = max(720, 210 + max(1, len(xs)) * 126), 120, 52, 180, 78
    height = max(220, top + max(1, len(ys)) * cell_h + 34)
    def esc(v: Any) -> str:
        return str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">', '<rect width="100%" height="100%" fill="#0b1220"/>', '<style>text{font-family:Segoe UI,Arial,sans-serif;fill:#e8f0fa;font-size:12px}.muted{fill:#9aacc4;font-size:11px}.title{font-size:18px;font-weight:600}</style>', f'<text class="title" x="22" y="32">{esc(title)}</text>']
    for j, x in enumerate(xs):
        parts.append(f'<text class="muted" x="{left + j*cell_w + 30}" y="60">{esc(x)}</text>')
    for i, y in enumerate(ys):
        yy = top + i * cell_h
        parts.append(f'<text class="muted" x="16" y="{yy + 31}">{esc(str(y)[:22])}</text>')
        for j, value in enumerate(matrix[i] if i < len(matrix) else []):
            if value is None:
                xx = left + j * cell_w
                parts.append(f'<rect x="{xx}" y="{yy}" width="{cell_w-6}" height="{cell_h-6}" rx="6" fill="#1b2c45"/>')
                continue
            numeric = float(value or 0)
            if percent:
                rate = max(0.0, min(1.0, numeric))
            else:
                finite_values = [float(v) for row in matrix for v in row if v is not None]
                scale = max(finite_values) if finite_values else 1.0
                rate = max(0.0, min(1.0, numeric / max(scale, 1e-9)))
            r, g, b = int(28 + 45 * rate), int(57 + 171 * rate), int(100 + 117 * rate)
            xx = left + j * cell_w
            parts.append(f'<rect x="{xx}" y="{yy}" width="{cell_w-6}" height="{cell_h-6}" rx="6" fill="rgb({r},{g},{b})"/>')
            label = f"{rate*100:.0f}%" if percent else f"{numeric:.1f}s"
            parts.append(f'<text x="{xx+38}" y="{yy+31}">{label}</text>')
    parts.append('</svg>')
    path = output / filename
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


def _write_png_heatmap(output: Path, heatmap: Mapping[str, Any], title: str, filename: str, percent: bool = True) -> Path | None:
    """Render a publication-friendly PNG when matplotlib is available."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except Exception:
        return None
    matrix = heatmap.get("matrix", [])
    if not matrix:
        return None
    fig, ax = plt.subplots(figsize=(8.6, 4.8), dpi=140)
    fig.patch.set_facecolor("#0b1220")
    ax.set_facecolor("#111c2e")
    values = np.array(matrix, dtype=float)
    finite = values[np.isfinite(values)]
    vmax = 1.0 if percent else (float(finite.max()) if finite.size else 1.0)
    image = ax.imshow(values, cmap="viridis", vmin=0, vmax=vmax, aspect="auto")
    ax.set_title(title)
    ax.set_xlabel(str(heatmap.get("x", "x")))
    ax.set_ylabel(str(heatmap.get("y", "y")))
    ax.set_xticks(range(len(heatmap.get("x_values", []))), labels=[str(v) for v in heatmap.get("x_values", [])])
    ax.set_yticks(range(len(heatmap.get("y_values", []))), labels=[str(v) for v in heatmap.get("y_values", [])])
    for i, row in enumerate(matrix):
        for j, value in enumerate(row):
            if value is not None:
                label = f"{float(value)*100:.0f}%" if percent else f"{float(value):.1f}s"
                ax.text(j, i, label, ha="center", va="center", color="white", fontsize=9)
    ax.title.set_color("#e8f0fa")
    ax.xaxis.label.set_color("#9aacc4"); ax.yaxis.label.set_color("#9aacc4")
    ax.tick_params(colors="#9aacc4")
    colorbar = fig.colorbar(image, ax=ax, label=(str(heatmap.get("metric", "value")) + ("" if percent else " (seconds)")))
    colorbar.ax.yaxis.label.set_color("#9aacc4")
    colorbar.ax.tick_params(colors="#9aacc4")
    fig.tight_layout()
    path = output / filename
    fig.savefig(path, facecolor="#0b1220")
    plt.close(fig)
    return path


def run_demo(
    runs: int = 100,
    seed: int = 20261004,
    output: Path | None = None,
    do_sweep: bool = True,
    formula: str | None = None,
    formula_variants: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Run the requested full demo: 3 builds, 3 enemies, one phase boss."""
    catalogue = load_demo_catalog()
    builds: list[Build] = catalogue["builds"]
    enemies: list[Enemy] = catalogue["enemies"]
    boss: Enemy = catalogue["bosses"][0]
    catalogue_formula = (catalogue.get("formula_defaults", {}) or {}).get("damage")
    effective_formula = _validated_formula(formula if formula is not None else catalogue_formula)
    config = _catalogue_simulation_config(
        catalogue, seed=seed, max_time=120.0, record_replay=False, formula=effective_formula
    )
    # The representative replay must use the same effective formula as the
    # aggregate scenarios; otherwise a custom Formula Lab study would export
    # metrics and a replay produced by different damage rules.
    replay_config = _catalogue_simulation_config(
        catalogue, seed=seed, max_time=120.0, record_replay=True, formula=config.formula
    )
    scenarios: list[dict[str, Any]] = []
    # The normal-enemy scenarios prove the same mechanisms work beyond a
    # single dummy; boss is the research target for the balance sweep.
    for enemy in enemies + [boss]:
        for index, build in enumerate(builds):
            scenario_seed = seed + index * 1000 + len(scenarios) * 31
            batch = run_batch(build, enemy, runs=runs, config=config, seed=scenario_seed, keep_fights=False)
            data = _batch_payload(batch, include_fights=False)
            # Keep exactly one complete replay per scenario, independent of the
            # aggregate run count.  This makes 10,000-run studies bounded in
            # memory while preserving a genuinely inspectable event stream.
            if not scenarios:
                from .core import run_fight
                sample = run_fight(build, enemy, replay_config, seed=scenario_seed)
                data["fights"] = [sample.to_dict()]
            data.update({"build_id": build.id, "build": build.name, "enemy_id": enemy.id, "enemy": enemy.name})
            data["warnings"] = detect_balance_warnings(build, enemy, result=data, formula=config.formula)
            scenarios.append(data)

    result: dict[str, Any] = {
        "project": "Game Mechanic Lab",
        "demo": {"builds": len(builds), "skills": len(catalogue["skills"]), "enemies": len(enemies), "bosses": len(catalogue["bosses"])},
        "runs_per_scenario": runs,
        "seed": seed,
        "scenarios": scenarios,
    }
    if do_sweep:
        # Parameter Sweep: three builds × four Boss HP tuning points.
        sweep_rows: list[dict[str, Any]] = []
        for build_index, build in enumerate(builds):
            rows = parameter_sweep(build, boss, {"enemy.stats.hp": [5000, 7500, 10000, 12500]}, runs=runs, seed=seed + build_index * 100000, config=config)
            for row in rows:
                row["build"] = build.name
                row["build_id"] = build.id
            sweep_rows.extend(rows)
        result["sweep"] = {
            "rows": sweep_rows,
            "heatmap": project_heatmap(sweep_rows, "enemy.stats.hp", "build", "win_rate"),
            "ttk_heatmap": project_heatmap(sweep_rows, "enemy.stats.hp", "build", "median_ttk"),
        }
        # A second projection answers the concrete tuning question "how much
        # player damage is needed for each boss HP?" rather than only comparing
        # named builds.
        damage_rows = parameter_sweep(
            builds[0], boss,
            # Include sub-baseline attack values so the chart exposes the
            # transition from under-tuned to over-tuned damage instead of
            # collapsing into a flat 100% win-rate block.
            {"player.stats.attack": [40, 80, 120, 160], "enemy.stats.hp": [5000, 7500, 10000, 12500]},
            runs=runs, seed=seed + 900000, config=config,
        )
        result["sweep"]["damage_rows"] = damage_rows
        # Keep both views: win rate answers the pass/fail question while TTK
        # exposes the much more useful gradient before a build becomes a
        # guaranteed win.
        result["sweep"]["damage_vs_boss_hp"] = project_heatmap(damage_rows, "player.stats.attack", "enemy.stats.hp", "median_ttk")
        result["sweep"]["damage_vs_boss_hp_win_rate"] = project_heatmap(damage_rows, "player.stats.attack", "enemy.stats.hp", "win_rate")
    if formula_variants:
        result["formula_comparison"] = compare_formula_variants(
            builds[0], boss, formula_variants, runs=runs, seed=seed, config=config
        )
    if output:
        write_demo_artifacts(output, result, builds[0], boss, config)
    return result


def write_demo_artifacts(output: Path, result: Mapping[str, Any], build: Build, boss: Enemy, config: SimulationConfig) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "demo_results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    scenarios = list(result.get("scenarios", []))
    if scenarios:
        first = scenarios[0]
        replay = (first.get("fights") or [{}])[0].get("events", [])
        (output / "replay_sample.json").write_text(json.dumps(replay, ensure_ascii=False, indent=2), encoding="utf-8")
    sweep = result.get("sweep", {})
    rows = list(sweep.get("rows", [])) if isinstance(sweep, Mapping) else []
    if rows:
        fields = ["build", "build_id", "enemy.stats.hp", "win_rate", "average_ttk", "average_dps", "average_damage_taken", "deaths", "player_deaths", "runs"]
        with (output / "balance_sweep.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
            writer.writeheader(); writer.writerows(rows)
        heatmap = sweep.get("heatmap", {})
        (output / "win_rate_heatmap.json").write_text(json.dumps(heatmap, ensure_ascii=False, indent=2), encoding="utf-8")
        _write_svg_heatmap(output, heatmap, "Boss HP × Player Build — Win Rate")
        _write_png_heatmap(output, heatmap, "Boss HP × Player Build — Win Rate", "win_rate_heatmap.png")
        ttk_heatmap = sweep.get("ttk_heatmap", {})
        if ttk_heatmap:
            (output / "ttk_heatmap.json").write_text(json.dumps(ttk_heatmap, ensure_ascii=False, indent=2), encoding="utf-8")
            _write_svg_heatmap(output, ttk_heatmap, "Boss HP × Player Build — Median TTK", "ttk_heatmap.svg", percent=False)
            _write_png_heatmap(output, ttk_heatmap, "Boss HP × Player Build — Median TTK", "ttk_heatmap.png", percent=False)
    damage_heatmap = sweep.get("damage_vs_boss_hp", {}) if isinstance(sweep, Mapping) else {}
    if damage_heatmap:
        (output / "damage_vs_boss_hp_heatmap.json").write_text(json.dumps(damage_heatmap, ensure_ascii=False, indent=2), encoding="utf-8")
        _write_svg_heatmap(output, damage_heatmap, "Player Damage × Boss HP — Median TTK", "damage_vs_boss_hp_heatmap.svg", percent=False)
        _write_png_heatmap(output, damage_heatmap, "Player Damage × Boss HP — Median TTK", "damage_vs_boss_hp_heatmap.png", percent=False)
    damage_win_rate = sweep.get("damage_vs_boss_hp_win_rate", {}) if isinstance(sweep, Mapping) else {}
    if damage_win_rate:
        (output / "damage_vs_boss_hp_win_rate_heatmap.json").write_text(json.dumps(damage_win_rate, ensure_ascii=False, indent=2), encoding="utf-8")
        _write_svg_heatmap(output, damage_win_rate, "Player Damage × Boss HP — Win Rate", "damage_vs_boss_hp_win_rate_heatmap.svg")
        _write_png_heatmap(output, damage_win_rate, "Player Damage × Boss HP — Win Rate", "damage_vs_boss_hp_win_rate_heatmap.png")
    formula_comparison = result.get("formula_comparison")
    if isinstance(formula_comparison, Mapping):
        _write_formula_comparison_artifacts(output, formula_comparison)
    record = ExperimentRecord(
        id="demo_balance_001", name="Clockwork Tyrant Balance Study",
        research_question="How do build archetype and boss HP change win rate and TTK?",
        hypothesis="Boss HP of 7,500–10,000 produces a readable fight without erasing build differences.",
        config={"player": build.to_dict(), "enemy": boss.to_dict(), "simulation": config.__dict__, "runs": result.get("runs_per_scenario")},
        runs=int(result.get("runs_per_scenario", 0)), results=dict(result),
        conclusion="Generated automatically; inspect heatmap and warnings before accepting a balance decision.",
    )
    save_experiment(record, output / "demo_experiment_record.json")
    (output / "demo_report.md").write_text(render_report(record), encoding="utf-8")


def run_single_batch(
    runs: int = 100,
    seed: int = 20261004,
    output: Path | None = None,
    formula: str | None = None,
    formula_variants: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Run one bounded combat sample for a headless smoke/benchmark command.

    ``--demo`` is intentionally the opt-in for the 12-scenario catalogue and
    sweep.  A bare ``--headless --runs 10000`` should therefore benchmark one
    real fight matchup instead of unexpectedly launching the entire demo (and
    its many sweep cells).  The same core simulator is used in both paths.
    """
    catalogue = load_demo_catalog()
    build = catalogue["builds"][0]
    enemy = catalogue["bosses"][0]
    catalogue_formula = (catalogue.get("formula_defaults", {}) or {}).get("damage")
    effective_formula = _validated_formula(formula if formula is not None else catalogue_formula)
    config = _catalogue_simulation_config(
        catalogue, seed=seed, max_time=120.0, record_replay=False, formula=effective_formula
    )
    batch = run_batch(build, enemy, runs=runs, config=config, seed=seed, keep_fights=False)
    # Capture one representative event stream separately so the command still
    # proves that the aggregate metrics come from the replay-capable engine.
    from .core import run_fight
    sample = run_fight(
        build,
        enemy,
        _catalogue_simulation_config(
            catalogue, seed=seed, max_time=120.0, record_replay=True, formula=config.formula
        ),
        seed=seed,
    )
    result = {
        "project": "Game Mechanic Lab",
        "mode": "single_batch",
        "build": build.name,
        "build_id": build.id,
        "enemy": enemy.name,
        "enemy_id": enemy.id,
        "runs": runs,
        "formula": config.formula,
        "summary": _batch_payload(batch, include_fights=False),
        "events": sample.events,
        "replay": sample.to_dict(),
    }
    if formula_variants:
        result["formula_comparison"] = compare_formula_variants(
            build, enemy, formula_variants, runs=runs, seed=seed, config=config
        )
    if output is not None:
        output.mkdir(parents=True, exist_ok=True)
        (output / "simulation_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        (output / "replay_sample.json").write_text(json.dumps(sample.events, ensure_ascii=False, indent=2), encoding="utf-8")
        comparison = result.get("formula_comparison")
        if isinstance(comparison, Mapping):
            _write_formula_comparison_artifacts(output, comparison)
    return result


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="GameMechanicLab", description="Game Mechanic Lab / 游戏机制与数值实验室")
    p.add_argument("--demo", action="store_true", help="run the bundled 3-build / 3-enemy / 1-boss experiment")
    p.add_argument("--headless", action="store_true", help="run without opening the desktop Arena")
    p.add_argument("--sweep", action="store_true", help="run the demo with the Boss HP balance sweep")
    p.add_argument("--runs", type=int, default=100, choices=(1, 100, 10000), help="fights per scenario or single matchup")
    p.add_argument("--seed", type=int, default=20261004, help="reproducible base random seed")
    p.add_argument("--formula", default=None, help="safe damage formula for the selected simulation")
    p.add_argument(
        "--formula-variant", action="append", default=[], metavar="NAME=EXPRESSION",
        help="compare a named safe formula variant; repeat for multiple variants",
    )
    p.add_argument("--output", type=Path, default=None, help="write JSON, CSV, SVG, replay, experiment and report files here")
    return p


def _emit_compact_json(payload: Mapping[str, Any]) -> None:
    """Write CLI output when a console is attached.

    PyInstaller's Windows ``--windowed`` bootloader can set ``sys.stdout`` to
    ``None``.  The packaged app still writes its requested JSON artifact, but
    must not fail merely because there is no console to receive the compact
    human/automation summary.
    """
    stream = getattr(sys, "stdout", None)
    if stream is not None:
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=stream)


def main(argv: list[str] | None = None) -> int:
    argument_parser = parser()
    args = argument_parser.parse_args(argv)
    try:
        formula_variants = _parse_formula_variants(args.formula_variant)
    except ValueError as exc:
        argument_parser.error(str(exc))
    no_display = sys.platform != "win32" and not os.environ.get("DISPLAY")
    if args.headless or args.sweep or args.output is not None or no_display:
        # The catalogue demo is explicit.  This matters for the supported
        # 10,000-run option: a plain headless benchmark remains quick and
        # bounded, while ``--demo`` deliberately evaluates every scenario and
        # sweep cell.
        if args.demo or args.sweep:
            result = run_demo(
                args.runs,
                args.seed,
                args.output,
                do_sweep=bool(args.sweep or args.demo),
                formula=args.formula,
                formula_variants=formula_variants or None,
            )
            first = result.get("scenarios", [{}])[0] if result.get("scenarios") else {}
            first_fight = (first.get("fights") or [{}])[0] if isinstance(first, Mapping) else {}
            compact = {
                "summary": {
                    "runs": first.get("runs", args.runs),
                    "wins": first.get("wins", 0),
                    "win_rate": first.get("win_rate", 0),
                    "avg_ttk": first.get("average_ttk", first.get("avg_ttk", 0)),
                    "avg_dps": first.get("average_dps", first.get("avg_dps", 0)),
                    "avg_damage_taken": first.get("average_damage_taken", first.get("avg_damage_taken", 0)),
                    "deaths": first.get("deaths", 0),
                    "simulation_mode": first.get("simulation_mode", "event_replay"),
                },
                "events": first_fight.get("events", []) if isinstance(first_fight, Mapping) else [],
                "demo": result["demo"], "runs_per_scenario": result["runs_per_scenario"],
                "scenario_count": len(result["scenarios"]), "sweep_cells": len(result.get("sweep", {}).get("rows", [])),
                "output": str(args.output or ""),
            }
            if "formula_comparison" in result:
                compact["formula_comparison"] = result["formula_comparison"]
            _emit_compact_json(compact)
            return 0

        result = run_single_batch(
            args.runs,
            args.seed,
            args.output,
            formula=args.formula,
            formula_variants=formula_variants or None,
        )
        summary = result["summary"]
        # Keep the compact machine-readable summary backwards-compatible with
        # the early launcher while exposing the selected matchup alongside it.
        compact = {
            "summary": {
                "runs": summary.get("runs", args.runs),
                "wins": summary.get("wins", 0),
                "win_rate": summary.get("win_rate", 0),
                "avg_ttk": summary.get("average_ttk", summary.get("avg_ttk", 0)),
                "avg_dps": summary.get("average_dps", summary.get("avg_dps", 0)),
                "avg_damage_taken": summary.get("average_damage_taken", summary.get("avg_damage_taken", 0)),
                "deaths": summary.get("deaths", 0),
                "simulation_mode": summary.get("simulation_mode", "event_replay"),
            },
            "events": result.get("events", []),
            "mode": result.get("mode", "single_batch"),
            "build": result.get("build"), "enemy": result.get("enemy"),
            "formula": result.get("formula"),
            "output": str(args.output or ""),
        }
        if "formula_comparison" in result:
            compact["formula_comparison"] = result["formula_comparison"]
        _emit_compact_json(compact)
        return 0
    # With a real desktop, ``--demo`` loads the bundled data into the Arena;
    # headless/CI callers opt into the batch runner explicitly above.
    from .ui.app import launch
    launch()
    return 0


__all__ = ["load_demo_catalog", "run_demo", "run_single_batch", "write_demo_artifacts", "main"]
