"""End-to-end contracts for sweep, heatmap, warning, and experiment modules."""

from __future__ import annotations

import pytest

from gamemechaniclab.core import (
    Build,
    compare_formula_variants,
    Enemy,
    ExperimentRecord,
    SimulationConfig,
    Skill,
    Stats,
    detect_balance_warnings,
    load_experiment,
    parameter_sweep,
    project_heatmap,
    render_report,
    save_experiment,
)
from gamemechaniclab.core.formula import FormulaError


def _fighter() -> Build:
    return Build(
        "sweep-player",
        stats=Stats(hp=500, attack=100, defense=10, attack_speed=2),
        skills=[Skill("attack", multiplier=1.0, cooldown=0.2)],
    )


def _dummy() -> Enemy:
    return Enemy("sweep-dummy", stats=Stats(hp=400, attack=0, defense=0), ai_style="passive")


def test_parameter_sweep_runs_every_cartesian_cell_without_mutating_inputs() -> None:
    player, enemy = _fighter(), _dummy()
    rows = parameter_sweep(
        player,
        enemy,
        parameters={
            "player.stats.attack": [50, 100],
            "enemy.stats.hp": {"start": 200, "stop": 600, "step": 200},
        },
        runs=1,
        seed=8,
        config=SimulationConfig(max_time=8, record_replay=False),
    )
    assert len(rows) == 6
    assert {(r["player.stats.attack"], r["enemy.stats.hp"]) for r in rows} == {
        (50, 200), (50, 400), (50, 600), (100, 200), (100, 400), (100, 600)
    }
    assert all({"win_rate", "average_dps", "median_ttk", "skill_usage", "player_deaths", "seed"} <= set(row) for row in rows)
    assert player.stats.attack == 100 and enemy.stats.hp == 400


def test_heatmap_projection_keeps_axes_and_metric_matrix() -> None:
    rows = parameter_sweep(
        _fighter(),
        _dummy(),
        parameters={"player.stats.attack": [60, 120], "enemy.stats.hp": [200, 500]},
        runs=1,
        seed=2,
        config=SimulationConfig(max_time=8, record_replay=False),
    )
    chart = project_heatmap(rows, "player.stats.attack", "enemy.stats.hp", metric="win_rate")
    assert chart["x_values"] == [60, 120]
    assert chart["y_values"] == [200, 500]
    assert len(chart["matrix"]) == 2
    assert all(len(line) == 2 for line in chart["matrix"])
    assert all(value in {0.0, 1.0} or value is None for line in chart["matrix"] for value in line)


def test_formula_variants_are_paired_safe_and_report_deltas() -> None:
    player = Build(
        "formula-player",
        stats=Stats(hp=200, attack=80, attack_speed=2),
        skills=[Skill("strike", multiplier=1.0, cooldown=0.2)],
    )
    enemy = Enemy("formula-dummy", stats=Stats(hp=160, attack=0, defense=10), ai_style="passive")
    config = SimulationConfig(max_time=5, seed=17, record_replay=True)
    comparison = compare_formula_variants(
        player,
        enemy,
        {
            "baseline": "damage = max(0, attack * multiplier - defense)",
            "armored": "max(0, attack * multiplier - defense * 2)",
        },
        runs=3,
        seed=17,
        config=config,
    )
    assert comparison["baseline"] == "baseline"
    assert comparison["paired_seed"] is True
    assert comparison["runs"] == 3
    assert [row["formula_id"] for row in comparison["formula_rows"]] == ["baseline", "armored"]
    assert comparison["formula_rows"][0]["formula"] == "max(0, attack * multiplier - defense)"
    assert comparison["formula_rows"][0]["delta_vs_baseline"]["win_rate"] == 0.0
    assert comparison["formula_rows"][1]["delta_vs_baseline"]["average_dps"] <= 0.0
    # The helper must not change the caller-owned replay setting or formula.
    assert config.record_replay is True
    assert config.formula == SimulationConfig().formula


def test_formula_sweep_axis_changes_config_without_mutating_base() -> None:
    player, enemy = _fighter(), _dummy()
    base = SimulationConfig(max_time=5, formula="attack * multiplier", record_replay=False)
    rows = parameter_sweep(
        player,
        enemy,
        parameters={"simulation.formula": ["attack * multiplier", "0"]},
        runs=1,
        seed=4,
        config=base,
    )
    assert [row["formula"] for row in rows] == ["attack * multiplier", "0"]
    assert rows[0]["win_rate"] == 1.0
    assert rows[1]["win_rate"] == 0.0
    assert base.formula == "attack * multiplier"


def test_formula_comparison_validates_every_variant_before_running() -> None:
    with pytest.raises(FormulaError):
        compare_formula_variants(
            _fighter(),
            _dummy(),
            {"good": "attack * multiplier", "bad": "__import__('os')"},
            runs=1,
            seed=1,
        )


def test_balance_warnings_cover_pathological_mechanics() -> None:
    problematic = Build(
        "broken-build",
        stats=Stats(hp=100, attack=-5, critical=1.0, cooldown_reduction=0.95),
        formula="attack - defense - 999",
        skills=[
            Skill("loop", damage=-1, cooldown=0.0, hitstun=1.0, combo_step=1, tags=["combo"]),
        ],
    )
    enemy = Enemy("wall", stats=Stats(hp=999999, defense=100))
    fake_batch = {
        "average_dps": 99999,
        "win_rate": 0.0,
        "fights": [{"win": False, "events": [{"type": "ComboStep"}] * 101}],
    }
    codes = {item["code"] for item in detect_balance_warnings(problematic, enemy, result=fake_batch)}
    assert {
        "100% Crit",
        "Cooldown Loop",
        "Permanent Stun",
        "Infinite Combo",
        "Negative Damage",
        "Unkillable Build",
        "DPS Explosion",
    } <= codes


def test_experiment_record_save_load_and_report(tmp_path) -> None:
    record = ExperimentRecord(
        id="exp-001",
        name="I-frame comparison",
        research_question="Does 0.5 s i-frame improve win rate?",
        hypothesis="The longer window reduces damage taken.",
        config={"seed": 42, "i_frame": [0.3, 0.5]},
        runs=100,
        results={"win_rate": 0.62, "average_damage_taken": 140},
        conclusion="Supported in the synthetic arena.",
    )
    path = save_experiment(record, tmp_path / "i_frame.json")
    restored = load_experiment(path)
    assert restored.to_dict() == record.to_dict()
    report = render_report(restored)
    assert "I-frame comparison" in report
    assert "Does 0.5 s i-frame improve win rate?" in report
    assert '"win_rate": 0.62' in report
