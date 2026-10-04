"""Headless launcher smoke test used by portable/CI builds."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_headless_cli_emits_metrics_and_events() -> None:
    proc = subprocess.run(
        [sys.executable, "app.py", "--demo", "--headless", "--runs", "1"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    payload = json.loads(proc.stdout)
    assert payload["summary"]["runs"] == 1
    assert {"win_rate", "avg_dps", "avg_ttk", "deaths"} <= set(payload["summary"])
    assert payload["events"], "headless demo must expose a real event stream"


def test_full_demo_cli_writes_experiment_sweep_replay_and_report(tmp_path: Path) -> None:
    output = tmp_path / "demo-artifacts"
    proc = subprocess.run(
        [sys.executable, "-m", "gamemechaniclab", "--demo", "--headless", "--runs", "1", "--output", str(output)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    summary = json.loads(proc.stdout)
    assert summary["demo"] == {"builds": 3, "skills": 5, "enemies": 3, "bosses": 1}
    assert summary["scenario_count"] == 12
    assert summary["sweep_cells"] == 12
    for name in (
        "demo_results.json",
        "balance_sweep.csv",
        "replay_sample.json",
        "demo_experiment_record.json",
        "demo_report.md",
        "win_rate_heatmap.json",
        "win_rate_heatmap.svg",
        "ttk_heatmap.json",
        "ttk_heatmap.png",
    ):
        assert (output / name).exists(), name


def test_cli_accepts_all_required_run_sizes() -> None:
    from gamemechaniclab.cli import parser

    for size in (1, 100, 10000):
        assert parser().parse_args(["--headless", "--runs", str(size)]).runs == size


def test_cli_formula_lab_runs_custom_formula_and_exports_comparison(tmp_path: Path) -> None:
    from gamemechaniclab.cli import parser, run_single_batch

    args = parser().parse_args(
        [
            "--headless",
            "--runs",
            "1",
            "--formula",
            "max(0, attack * multiplier - defense)",
            "--formula-variant",
            "baseline=attack * multiplier",
            "--formula-variant",
            "safe=max(0, attack * multiplier - defense * 1.2)",
        ]
    )
    assert args.formula.startswith("max(0")
    assert args.formula_variant == ["baseline=attack * multiplier", "safe=max(0, attack * multiplier - defense * 1.2)"]
    result = run_single_batch(
        runs=1,
        seed=9,
        output=tmp_path,
        formula=args.formula,
        formula_variants={"baseline": "attack * multiplier", "safe": "max(0, attack * multiplier - defense * 1.2)"},
    )
    assert result["formula"] == args.formula
    assert [row["formula_id"] for row in result["formula_comparison"]["formula_rows"]] == ["baseline", "safe"]
    assert (tmp_path / "formula_comparison.json").exists()
    assert (tmp_path / "formula_comparison.csv").exists()


def test_cli_rejects_unsafe_formula_before_simulation() -> None:
    from gamemechaniclab.cli import run_single_batch
    from gamemechaniclab.core.formula import FormulaError

    try:
        run_single_batch(runs=1, seed=2, formula="__import__('os').system('echo unsafe')")
    except FormulaError:
        return
    raise AssertionError("unsafe formula should be rejected before a combat run")
