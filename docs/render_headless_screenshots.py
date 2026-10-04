#!/usr/bin/env python3
"""Render reproducible screenshots from a real headless Demo run.

The Tk capture helper is used on a graphical desktop.  CI/container sessions
often have no display, so this companion produces report screenshots from the
same ``demo_results.json`` and replay events without inventing sample values.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=Path("artifacts/final_demo"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/screenshots"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except Exception as exc:  # pragma: no cover - optional chart dependency
        print(f"matplotlib is required for headless screenshots: {exc}")
        return 2

    result = json.loads((args.input / "demo_results.json").read_text(encoding="utf-8"))
    replay = json.loads((args.input / "replay_sample.json").read_text(encoding="utf-8"))
    scenarios = result.get("scenarios", [])
    first = scenarios[0] if scenarios else {}
    boss = next((row for row in scenarios if row.get("enemy_id") == "clockwork_tyrant" and row.get("build_id") == "balanced_vanguard"), first)

    plt.rcParams.update({"font.family": "DejaVu Sans", "axes.facecolor": "#111c2e", "figure.facecolor": "#0b1220", "text.color": "#e8f0fa", "axes.labelcolor": "#9aacc4", "xtick.color": "#9aacc4", "ytick.color": "#9aacc4", "axes.edgecolor": "#263a55"})
    fig = plt.figure(figsize=(16, 9), dpi=120)
    grid = fig.add_gridspec(2, 3, height_ratios=(0.8, 2.4), hspace=0.34, wspace=0.25)
    fig.suptitle("GAME MECHANIC LAB  /  COMBAT BALANCE WORKBENCH", x=0.04, y=0.97, ha="left", fontsize=20, fontweight="bold")
    fig.text(0.04, 0.932, "Headless capture from the real event-driven combat simulator", color="#9aacc4", fontsize=10)

    cards = [("WIN RATE", f"{float(boss.get('win_rate', 0))*100:.1f}%", "#64df9a"), ("AVG DPS", f"{float(boss.get('average_dps', 0)):.1f}", "#63d5ff"), ("AVG TTK", f"{float(boss.get('average_ttk', 0)):.1f}s", "#f4c56a")]
    for idx, (label, value, color) in enumerate(cards):
        ax = fig.add_subplot(grid[0, idx]); ax.set_facecolor("#16243a"); ax.axis("off")
        ax.text(0.06, 0.70, label, transform=ax.transAxes, color="#9aacc4", fontsize=9, fontweight="bold")
        ax.text(0.06, 0.20, value, transform=ax.transAxes, color=color, fontsize=25, fontweight="bold")
        for spine in ax.spines.values(): spine.set_visible(True); spine.set_edgecolor("#263a55")

    timeline = fig.add_subplot(grid[1, :2])
    times, colors, labels = [], [], []
    palette = {"Damage": "#63d5ff", "Critical": "#f4c56a", "Dodge": "#b99cff", "SkillCast": "#64df9a", "Death": "#ff7088", "BuffApplied": "#64df9a", "DebuffApplied": "#ff7088"}
    for event in replay:
        kind = str(event.get("type", "Event"))
        if kind in palette:
            times.append(float(event.get("time", 0))); colors.append(palette[kind]); labels.append(kind)
    y = np.arange(len(times))
    timeline.scatter(times, y, c=colors, s=22, alpha=0.9)
    timeline.set_title("Combat Replay · event timeline", loc="left", fontweight="bold")
    timeline.set_xlabel("time (seconds)"); timeline.set_ylabel("ordered event")
    timeline.grid(axis="x", color="#263a55", alpha=.6)
    timeline.set_yticks([])
    seen = []
    for label, color in zip(labels, colors):
        if label not in seen:
            timeline.scatter([], [], c=color, label=label); seen.append(label)
    timeline.legend(loc="upper left", ncol=3, fontsize=8, frameon=False)

    heat = fig.add_subplot(grid[1, 2])
    chart = result.get("sweep", {}).get("heatmap", {})
    matrix = np.asarray(chart.get("matrix", []), dtype=float)
    if matrix.size:
        image = heat.imshow(matrix, cmap="viridis", vmin=0, vmax=1, aspect="auto")
        heat.set_xticks(range(len(chart.get("x_values", []))), labels=[str(v) for v in chart.get("x_values", [])], rotation=35, ha="right")
        heat.set_yticks(range(len(chart.get("y_values", []))), labels=[str(v) for v in chart.get("y_values", [])])
        for i, row in enumerate(matrix):
            for j, value in enumerate(row):
                heat.text(j, i, f"{value*100:.0f}%", ha="center", va="center", fontsize=8)
        fig.colorbar(image, ax=heat, fraction=.046, pad=.04)
    heat.set_title("Boss HP × Build · win rate", loc="left", fontweight="bold")

    path = args.output / "01_combat_arena_headless.png"
    fig.savefig(path, facecolor="#0b1220", bbox_inches="tight")
    plt.close(fig)
    # Keep the two exact heatmap exports alongside the dashboard for quick
    # inspection and to make the screenshot folder useful without the report.
    for source, target in (("win_rate_heatmap.png", "02_win_rate_heatmap.png"), ("ttk_heatmap.png", "03_ttk_heatmap.png"), ("damage_vs_boss_hp_heatmap.png", "04_damage_vs_boss_hp_heatmap.png"), ("damage_vs_boss_hp_win_rate_heatmap.png", "05_damage_vs_boss_hp_win_rate_heatmap.png")):
        source_path = args.input / source
        if source_path.exists():
            target_path = args.output / target
            target_path.write_bytes(source_path.read_bytes())
    (args.output / "README.md").write_text("These PNGs are reproducible headless captures rendered from artifacts/final_demo. The dashboard timeline and metrics come from the real CombatEvent replay; on a desktop, docs/capture_screenshots.py can capture the live Tk Arena itself.\n", encoding="utf-8")
    print(f"wrote screenshots to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
