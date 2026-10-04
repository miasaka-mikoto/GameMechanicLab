"""Data-driven JSON/YAML loading helpers.

JSON is the zero-dependency canonical format (and is valid YAML 1.2).  When
PyYAML is installed, richer YAML files are accepted as well; otherwise a clear
error points callers to the paired JSON asset instead of silently dropping
nested values.
"""

from __future__ import annotations

import json
import copy
from pathlib import Path
from typing import Any, Mapping


def load_data(path: str | Path) -> Any:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if target.suffix.lower() == ".json":
        return json.loads(text)
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        # JSON is a valid YAML subset, so retain a useful fallback for paired
        # assets rather than implementing an unsafe ad-hoc YAML evaluator.
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise RuntimeError("YAML loading requires PyYAML; use the paired JSON file or install the optional dependency") from exc
    return yaml.safe_load(text)


def save_json(data: Any, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def load_project(root: str | Path) -> dict[str, Any]:
    """Load the bundled catalogues from a project/config directory."""
    root = Path(root)
    config = root / "configs" if (root / "configs").exists() else root
    names = ("mechanics", "skills", "builds", "enemies", "bosses")
    result: dict[str, Any] = {}
    for name in names:
        candidate = config / f"{name}.json"
        if not candidate.exists():
            candidate = config / f"{name}.yaml"
        if candidate.exists():
            result[name] = load_data(candidate)
    resolve_catalogue_references(result)
    return result


def _resolve_status_effect(value: Any, registry: Mapping[str, Any]) -> Any:
    """Expand a status-effect ID while preserving inline overrides."""
    if isinstance(value, str):
        base = copy.deepcopy(registry.get(value, {})) if isinstance(registry.get(value), Mapping) else {}
        base.setdefault("id", value)
        return base if base else value
    if not isinstance(value, Mapping):
        return value
    item = dict(value)
    key = str(item.get("id", item.get("effect", "")))
    if key and isinstance(registry.get(key), Mapping):
        merged = copy.deepcopy(dict(registry[key]))
        merged.update(item)
        merged.setdefault("id", key)
        item = merged
    # The mechanics registry uses readable per-tick names; normalize them to
    # the model's canonical magnitude field without discarding overrides.
    if "magnitude" not in item:
        if "damage_per_tick" in item:
            item["magnitude"] = item["damage_per_tick"]
        elif "heal_per_tick" in item:
            item["magnitude"] = item["heal_per_tick"]
    modifiers = dict(item.get("modifiers", {}) or {})
    for key, value in item.items():
        if key.endswith("_multiplier"):
            modifiers.setdefault(key, value)
        elif key.endswith("_flat"):
            modifiers.setdefault(key[:-5], value)
    if modifiers:
        item["modifiers"] = modifiers
    return item


def _resolve_skill(value: Any, skill_map: Mapping[str, Any], registry: Mapping[str, Any]) -> Any:
    if isinstance(value, str):
        value = copy.deepcopy(skill_map.get(value, {"id": value}))
    if not isinstance(value, Mapping):
        return value
    item = copy.deepcopy(dict(value))
    raw_effects = item.get("status_effects", item.get("statusEffects", [])) or []
    if isinstance(raw_effects, Mapping) or isinstance(raw_effects, str):
        raw_effects = [raw_effects]
    item["status_effects"] = [_resolve_status_effect(effect, registry) for effect in raw_effects]
    return item


def resolve_catalogue_references(result: dict[str, Any]) -> dict[str, Any]:
    """Resolve skill/status IDs for callers using :func:`load_project`.

    The on-disk files intentionally remain compact and human-editable.  This
    adapter expands references at the data boundary so a skill can say
    ``status_effects: [burn]`` without silently becoming a zero-magnitude buff.
    """
    mechanics = result.get("mechanics") if isinstance(result.get("mechanics"), Mapping) else {}
    registry = mechanics.get("status_effects", {}) if isinstance(mechanics, Mapping) else {}
    if not isinstance(registry, Mapping):
        registry = {}
    skills_doc = result.get("skills") if isinstance(result.get("skills"), Mapping) else {}
    raw_skills = skills_doc.get("skills", []) if isinstance(skills_doc, Mapping) else []
    expanded_skills = [_resolve_skill(item, {}, registry) for item in raw_skills]
    if isinstance(skills_doc, dict):
        skills_doc["skills"] = expanded_skills
    skill_map = {str(item.get("id")): item for item in expanded_skills if isinstance(item, Mapping) and item.get("id") is not None}

    def expand_entity_doc(doc: Any) -> None:
        if not isinstance(doc, dict):
            return
        extra = doc.get("enemy_skills", {}) or {}
        if isinstance(extra, Mapping):
            skill_map.update({str(key): _resolve_skill(dict(value, id=key) if isinstance(value, Mapping) else {"id": key}, skill_map, registry) for key, value in extra.items()})
            doc["enemy_skills"] = {key: skill_map[str(key)] for key in extra}
        extra_boss = doc.get("boss_skills", {}) or {}
        if isinstance(extra_boss, Mapping):
            skill_map.update({str(key): _resolve_skill(dict(value, id=key) if isinstance(value, Mapping) else {"id": key}, skill_map, registry) for key, value in extra_boss.items()})
            doc["boss_skills"] = {key: skill_map[str(key)] for key in extra_boss}
        for entity in doc.get("builds", []) + doc.get("enemies", []) + doc.get("bosses", []):
            if not isinstance(entity, dict):
                continue
            entity["skills"] = [_resolve_skill(skill, skill_map, registry) for skill in (entity.get("skills", []) or [])]
            for phase in entity.get("phases", []) or []:
                if not isinstance(phase, dict):
                    continue
                # BossPhase.from_dict accepts all three spellings.  Resolve
                # IDs before model construction so compact ``add_status:
                # haste`` entries receive the same registry defaults as the
                # canonical ``status_effects`` list.
                status_key = next(
                    (key for key in ("status_effects", "add_status", "addStatus") if key in phase),
                    None,
                )
                if status_key is not None:
                    raw_statuses = phase.get(status_key) or []
                    if isinstance(raw_statuses, (Mapping, str)):
                        raw_statuses = [raw_statuses]
                    phase[status_key] = [_resolve_status_effect(effect, registry) for effect in raw_statuses]

    expand_entity_doc(result.get("enemies"))
    expand_entity_doc(result.get("bosses"))
    # Build documents reference the common skills catalogue.
    builds_doc = result.get("builds")
    if isinstance(builds_doc, dict):
        for build in builds_doc.get("builds", []) or []:
            if isinstance(build, dict):
                build["skills"] = [_resolve_skill(skill, skill_map, registry) for skill in (build.get("skills", []) or [])]

    return result


__all__ = ["load_data", "save_json", "load_project", "resolve_catalogue_references"]
