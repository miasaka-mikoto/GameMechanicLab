"""Contract tests for the bundled, data-driven demo experiment."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gamemechaniclab.core.data import load_data, load_project


ROOT = Path(__file__).resolve().parents[1]


def _read(path: Path):
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return json.loads(text)
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(text)


def test_demo_manifest_counts_and_references() -> None:
    manifest = _read(ROOT / "demo" / "manifest.json")
    assert manifest["project"] == "Game Mechanic Lab"
    assert manifest["demo_counts"] == {"builds": 3, "skills": 5, "enemies": 3, "bosses": 1}
    for rel in [manifest["experiment"], manifest["sweep"], *manifest["configs"].values()]:
        assert (ROOT / rel).exists(), rel


def test_demo_catalog_has_required_dimensions() -> None:
    builds = _read(ROOT / "configs" / "builds.json")["builds"]
    skills = _read(ROOT / "configs" / "skills.json")["skills"]
    enemies = _read(ROOT / "configs" / "enemies.json")["enemies"]
    bosses = _read(ROOT / "configs" / "bosses.json")["bosses"]
    assert len(builds) == 3 and len(skills) == 5 and len(enemies) == 3 and len(bosses) == 1
    for build in builds:
        assert build["id"] and build["stats"]["hp"] > 0 and build["skills"]
    for skill in skills:
        assert skill["id"] and "multiplier" in skill and "cooldown" in skill
    for enemy in enemies:
        assert enemy["id"] and enemy["stats"]["hp"] > 0 and "ai_style" in enemy
    boss = bosses[0]
    assert boss.get("is_boss", True) and len(boss.get("phases", [])) == 3


def test_sweep_is_cartesian_and_has_heatmap_specs() -> None:
    sweep = _read(ROOT / "demo" / "sweep.json")
    dims = sweep["dimensions"]
    raw_cells = 1
    for dim in dims:
        raw_cells *= len(dim["values"])
    assert raw_cells == 72
    assert sweep["max_combinations"] == 36  # filters reduce executable cells
    assert {h["value"] for h in sweep["heatmaps"]} >= {"win_rate", "median_ttk"}
    assert set(sweep["metrics"]) >= {"win_rate", "median_ttk", "mean_dps"}


def test_json_and_yaml_assets_have_same_semantic_identity() -> None:
    for stem in ("mechanics", "skills", "builds", "enemies", "bosses"):
        json_obj = _read(ROOT / "configs" / f"{stem}.json")
        yaml_obj = _read(ROOT / "configs" / f"{stem}.yaml")
        assert json_obj == yaml_obj, stem
    assert _read(ROOT / "demo" / "experiment.json")["id"] == _read(ROOT / "demo" / "experiment.yaml")["id"]
    assert _read(ROOT / "demo" / "sweep.json")["id"] == _read(ROOT / "demo" / "sweep.yaml")["id"]


def test_public_data_loader_reads_a_project_catalogue() -> None:
    project = load_project(ROOT)
    assert {"mechanics", "skills", "builds", "enemies", "bosses"} <= set(project)
    assert len(project["builds"]["builds"]) == 3
    assert load_data(ROOT / "configs" / "skills.json")["skills"][0]["id"] == "basic_strike"


def test_public_loader_expands_status_and_skill_references() -> None:
    project = load_project(ROOT)
    ember = next(skill for skill in project["skills"]["skills"] if skill["id"] == "ember_trap")
    assert ember["status_effects"][0]["id"] == "burn"
    assert ember["status_effects"][0]["magnitude"] == 8.0
    first_build_skill = project["builds"]["builds"][0]["skills"][0]
    assert isinstance(first_build_skill, dict) and first_build_skill["id"] == "basic_strike"
