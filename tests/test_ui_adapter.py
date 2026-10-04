"""Smoke test the Arena's core-to-UI adapter without opening a window."""

from __future__ import annotations

from gamemechaniclab.ui.app import DEFAULT_BUILDS, DEFAULT_ENEMIES, EngineAdapter, GameMechanicLabApp, _CoreSimulationAdapter, _replay_snapshot


def test_arena_adapter_runs_core_fight_and_returns_replay() -> None:
    adapter = EngineAdapter()
    output = adapter.run_fights(
        {
            "build": DEFAULT_BUILDS[0],
            "enemy": DEFAULT_ENEMIES[0],
            "skills": [],
            "seed": 12,
        },
        runs=1,
        capture=True,
    )
    assert output["summary"]["runs"] == 1
    assert {"win_rate", "avg_dps", "avg_ttk", "deaths"} <= set(output["summary"])
    assert output["events"], "Arena replay must come from a real event-driven fight"
    assert {"actor", "target", "skill", "value"} <= set(output["events"][0])
    assert adapter.name in {"_CoreSimulationAdapter", "DemoEngine"}


def test_arena_adapter_surfaces_balance_warnings() -> None:
    adapter = EngineAdapter()
    config = {
        "build": {
            "id": "warning-build",
            "name": "Warning Build",
            "hp": 100,
            "attack": 100,
            "crit": 1.0,
            "skills": ["loop"],
        },
        "enemy": {"id": "dummy", "hp": 200, "defense": 0, "attack": 0, "ai_style": "passive"},
        "skills": [{"id": "loop", "multiplier": 1, "cooldown": 0}],
        "seed": 4,
    }
    output = adapter.run_fights(config, runs=1, capture=False)
    warnings = adapter.detect_warnings(config, output)
    codes = {w.get("type", w.get("code")) for w in warnings}
    assert "100% Crit" in codes
    assert "Cooldown Loop" in codes


def test_ui_sweep_preserves_formula_and_simulation_overrides() -> None:
    """Every sweep cell must use the same overrides as an Arena fight."""
    adapter = EngineAdapter()
    output = adapter.run_sweep(
        {
            "builds": [{"id": "p", "name": "P", "hp": 1000, "attack": 100, "skills": []}],
            "enemy": {"id": "e", "name": "E", "hp": 500, "attack": 0, "defense": 0, "ai_style": "passive"},
            "boss_hp_values": [500],
            "skills": [],
            "enemy_skills": [],
            "formula": "0",
            "simulation": {"max_time": 1, "starting_distance": 0, "allow_movement": False},
        },
        runs=1,
    )
    cell = output["cells"][0]
    assert cell["win_rate"] == 0.0
    assert cell["avg_dps"] == 0.0
    assert cell["avg_ttk"] == 1.0


def test_editor_stats_sync_into_canonical_nested_catalogue() -> None:
    item = {"hp": 750, "attack": 123, "critical": 0.4, "stats": {"hp": 1000, "attack": 80, "critical_chance": 0.1, "max_hp": 1000}}
    GameMechanicLabApp._sync_editor_stats(item, ("hp", "attack", "critical"))
    assert item["stats"]["hp"] == 750
    assert item["stats"]["max_hp"] == 750
    assert item["stats"]["attack"] == 123
    assert item["stats"]["critical_chance"] == 0.4


def test_ui_flat_hp_override_updates_nested_capacity() -> None:
    stats = _CoreSimulationAdapter._stats({"stats": {"hp": 1000, "max_hp": 1000}, "hp": 500})
    assert stats["hp"] == 500
    assert stats["max_hp"] == 500


def test_replay_event_line_formats_numeric_time() -> None:
    line = GameMechanicLabApp._event_line({"time": 0.25, "type": "Damage", "actor": "P", "target": "E", "value": 12})
    assert "0.25s" in line and "Damage" in line and "12" in line


def test_replay_snapshot_tracks_event_prefix_and_not_future_damage() -> None:
    events = [
        {"time": 0.0, "type": "SkillCast", "actor": "hero", "target": "boss", "skill": "slash"},
        {"time": 0.2, "type": "Critical", "actor": "hero", "target": "boss", "value": 80},
        {"time": 0.2, "type": "Damage", "actor": "hero", "target": "boss", "value": 50},
        {"time": 0.5, "type": "Damage", "actor": "boss", "target": "hero", "value": 12},
        {"time": 0.7, "type": "Dodge", "actor": "hero", "target": "boss"},
    ]
    frame = _replay_snapshot(events, 2, "hero", "boss", 100, 200)
    assert frame["enemy_hp"] == 150
    assert frame["player_hp"] == 100
    assert frame["damage_dealt"] == 50
    assert frame["damage_taken"] == 0
    assert frame["critical_hits"] == 1 and frame["skills"] == 1
    final = _replay_snapshot(events, 99, "hero", "boss", 100, 200)
    assert final["player_hp"] == 88 and final["dodges"] == 1
