"""Data models used by the Game Mechanic Lab simulation.

Models are intentionally plain dataclasses.  They can be constructed directly in
Python or loaded from the JSON/YAML-compatible dictionaries used by the UI.  No
game values are hidden in the engine; defaults are conservative, documented
starting values that callers can override in configuration files.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any, Dict, Iterable, List, Mapping, Optional
import copy


def _num(value: Any, default: float = 0.0) -> float:
    """Coerce a user-facing number while accepting empty/null values."""
    if value is None or value == "":
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _int(value: Any, default: int = 0) -> int:
    if value is None or value == "":
        return int(default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "on", "1"}:
            return True
        if lowered in {"false", "no", "off", "0", ""}:
            return False
    return bool(value)


@dataclass
class Stats:
    """Combat attributes shared by players and enemies.

    ``iframe`` is the duration in seconds granted after a dodge.  ``dodge`` and
    ``critical`` are probabilities represented as either fractions (0.2) or
    percentages (20); parsers normalize percentages to fractions.
    """

    hp: float = 100.0
    max_hp: Optional[float] = None
    attack: float = 10.0
    defense: float = 0.0
    critical: float = 0.05
    crit_multiplier: float = 1.5
    attack_speed: float = 1.0
    move_speed: float = 5.0
    cooldown_reduction: float = 0.0
    mana: float = 100.0
    max_mana: Optional[float] = None
    mana_regen: float = 0.0
    stamina: float = 100.0
    max_stamina: Optional[float] = None
    stamina_regen: float = 0.0
    iframe: float = 0.3
    dodge: float = 0.0
    knockback: float = 0.0
    hitstun: float = 0.0
    # Defensive tuning hooks remain part of the data model instead of being
    # inferred from a particular build or hard-coded in the resolver.
    knockback_resistance: float = 0.0
    hitstun_resistance: float = 0.0
    shield_received: float = 0.0
    resistances: Dict[str, float] = field(default_factory=dict)
    # Conditional execution-style damage bonus.  A value of ``0.12`` means
    # +12% damage while the target is below half of its maximum HP.  Keep this
    # new field at the end of the dataclass to preserve positional construction
    # compatibility for the pre-existing stats fields.
    damage_below_half_hp: float = 0.0

    def __post_init__(self) -> None:
        if self.max_hp is None:
            self.max_hp = float(self.hp)
        if self.max_mana is None:
            self.max_mana = float(self.mana)
        if self.max_stamina is None:
            self.max_stamina = float(self.stamina)
        self.hp = max(0.0, float(self.hp))
        self.max_hp = max(0.0, float(self.max_hp))
        # A data edit may provide a current resource above its declared
        # capacity.  Normalize the capacity upward instead of silently
        # clipping the starting value; threshold formulas then see the same
        # state the editor displayed.
        self.max_hp = max(self.max_hp, self.hp)
        self.mana = max(0.0, float(self.mana))
        self.max_mana = max(0.0, float(self.max_mana))
        self.max_mana = max(self.max_mana, self.mana)
        self.stamina = max(0.0, float(self.stamina))
        self.max_stamina = max(0.0, float(self.max_stamina))
        self.max_stamina = max(self.max_stamina, self.stamina)
        self.damage_below_half_hp = _num(self.damage_below_half_hp)
        self.critical = _probability(self.critical)
        self.dodge = _probability(self.dodge)
        self.cooldown_reduction = min(0.95, max(0.0, _probability(self.cooldown_reduction)))
        self.knockback_resistance = _probability(self.knockback_resistance)
        self.hitstun_resistance = _probability(self.hitstun_resistance)
        self.shield_received = _signed_probability(self.shield_received)
        raw_resistances = self.resistances or {}
        self.resistances = {
            str(k): _signed_probability(v) for k, v in raw_resistances.items()
        } if isinstance(raw_resistances, Mapping) else {}

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "Stats":
        data = dict(data or {})
        # Support the short names commonly used in config files.
        aliases = {
            "maxHP": "max_hp", "max-hp": "max_hp", "HP": "hp",
            "ATK": "attack", "DEF": "defense", "crit": "critical",
            "damageBelowHalfHp": "damage_below_half_hp",
            "damageBelowHalfHP": "damage_below_half_hp",
            "damage_below_half": "damage_below_half_hp",
            "critChance": "critical", "critical_chance": "critical", "crit_damage": "crit_multiplier", "critDamage": "crit_multiplier",
            "critical_multiplier": "crit_multiplier", "criticalMultiplier": "crit_multiplier",
            "attackSpeed": "attack_speed", "moveSpeed": "move_speed",
            "cooldownReduction": "cooldown_reduction", "maxMana": "max_mana",
            "manaRegen": "mana_regen", "maxStamina": "max_stamina",
            "staminaRegen": "stamina_regen", "iFrame": "iframe", "i_frame": "iframe",
            "dodgeChance": "dodge", "dodge_chance": "dodge", "knockBack": "knockback",
            "hitStun": "hitstun", "knockbackResistance": "knockback_resistance",
            "hitstunResistance": "hitstun_resistance", "stagger_resistance": "hitstun_resistance",
            "staggerResistance": "hitstun_resistance", "shieldReceived": "shield_received",
        }
        for old, new in aliases.items():
            if old in data and new not in data:
                data[new] = data[old]
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        values = {k: data[k] for k in allowed if k in data}
        for key in ("hp", "max_hp", "attack", "defense", "damage_below_half_hp", "crit_multiplier", "attack_speed",
                    "move_speed", "mana", "max_mana", "mana_regen", "stamina", "max_stamina",
                    "stamina_regen", "iframe", "knockback", "hitstun", "knockback_resistance",
                    "hitstun_resistance", "shield_received"):
            if key in values:
                values[key] = _num(values[key])
        for key in ("critical", "dodge", "cooldown_reduction"):
            if key in values:
                values[key] = _num(values[key])
        return cls(**values)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def clone(self, **changes: Any) -> "Stats":
        return replace(self, **changes)


def _probability(value: Any) -> float:
    v = _num(value)
    # Config authors often write 25 for 25%.  Values between 1 and 100 are
    # interpreted as percentages; values above 100 clamp to one.  The signed
    # resistance variant below uses the same magnitude-based conversion.
    if abs(v) > 1.0:
        v /= 100.0
    return min(1.0, max(0.0, v))


def _signed_probability(value: Any) -> float:
    """Normalize a probability-like value while preserving vulnerability.

    Most probabilities clamp at zero, whereas damage resistances intentionally
    allow negative values.  Keeping this helper next to ``_probability`` makes
    the percentage convention explicit without changing critical/dodge rules.
    """
    v = _num(value)
    if abs(v) > 1.0:
        v /= 100.0
    return min(1.0, max(-1.0, v))


@dataclass
class StatusEffect:
    id: str
    name: str = ""
    kind: str = "buff"  # buff, debuff, dot, hot, stun, shield
    duration: float = 0.0
    magnitude: float = 0.0
    tick_interval: float = 1.0
    stacks: int = 1
    max_stacks: int = 1
    modifiers: Dict[str, float] = field(default_factory=dict)
    damage_type: str = "physical"
    source_id: str = ""
    # Config files may express this as ``chance`` (0.75 or 75).  Keeping the
    # normalized value on the effect makes status application data-driven while
    # preserving the original event schema for replay consumers.
    apply_chance: float = 1.0

    def __post_init__(self) -> None:
        self.id = str(self.id)
        self.kind = str(self.kind or "buff").lower()
        self.apply_chance = _probability(self.apply_chance)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StatusEffect":
        d = dict(data)
        aliases = {"type": "kind", "effect": "kind", "tickInterval": "tick_interval",
                   "maxStacks": "max_stacks", "damageType": "damage_type", "source": "source_id",
                   "amount": "magnitude", "value": "magnitude",
                   "damage_per_tick": "magnitude", "damagePerTick": "magnitude",
                   "heal_per_tick": "magnitude", "healPerTick": "magnitude",
                   "chance": "apply_chance"}
        for old, new in aliases.items():
            if old in d and new not in d:
                d[new] = d[old]
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        vals = {k: d[k] for k in allowed if k in d}
        if "id" not in vals:
            vals["id"] = vals.get("name", "effect")
        for key in ("duration", "magnitude", "tick_interval"):
            if key in vals:
                vals[key] = _num(vals[key])
        for key in ("stacks", "max_stacks"):
            if key in vals:
                vals[key] = _int(vals[key], 1)
        if "apply_chance" in vals:
            vals["apply_chance"] = _probability(vals["apply_chance"])
        raw_modifiers = vals.get("modifiers", {}) or {}
        normalized_modifiers = {str(k): _num(v) for k, v in raw_modifiers.items()} if isinstance(raw_modifiers, Mapping) else {}
        # Registry-friendly aliases such as ``defense_flat`` and
        # ``attack_speed_multiplier`` are promoted into the canonical modifier
        # map instead of being silently ignored by the runtime stat resolver.
        for key, value in d.items():
            if key.endswith("_multiplier"):
                normalized_modifiers.setdefault(key, _num(value))
            elif key.endswith("_flat"):
                normalized_modifiers.setdefault(key[:-5], _num(value))
        vals["modifiers"] = normalized_modifiers
        return cls(**vals)


@dataclass
class Skill:
    id: str
    name: str = ""
    damage: float = 0.0
    multiplier: float = 1.0
    cooldown: float = 1.0
    cast_time: float = 0.0
    recovery: float = 0.0
    range: float = 2.0
    projectile_speed: float = 0.0
    aoe: float = 0.0
    cost: float = 0.0
    cost_type: str = "mana"
    status_effects: List[StatusEffect] = field(default_factory=list)
    hits: int = 1
    damage_type: str = "physical"
    dot: float = 0.0
    dot_duration: float = 0.0
    hot: float = 0.0
    hot_duration: float = 0.0
    knockback: float = 0.0
    hitstun: float = 0.0
    iframe: float = 0.0
    combo_step: int = 0
    tags: List[str] = field(default_factory=list)
    # Some skills (for example a burn field) intentionally cannot critical-hit.
    # Keep the rule in the declarative skill record instead of inferring it from
    # a UI tag or hard-coding an ID in the simulator.
    crit_allowed: bool = True
    # ``enemy`` is the default offensive target.  ``self`` makes defensive
    # skills (shield, heal, haste) affect their caster instead of accidentally
    # buffing the opponent in a two-actor arena.
    target: str = "enemy"

    def __post_init__(self) -> None:
        self.id = str(self.id)
        self.target = str(self.target or "enemy").lower()
        self.crit_allowed = _bool(self.crit_allowed, True)
        self.status_effects = [
            effect if isinstance(effect, StatusEffect)
            else StatusEffect.from_dict(effect) if isinstance(effect, Mapping)
            else StatusEffect(str(effect))
            for effect in (self.status_effects or [])
        ]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Skill":
        # Compact catalogues and editor forms may reference a skill by ID.
        if isinstance(data, str):
            return cls(id=str(data), name=str(data))
        d = dict(data)
        aliases = {
            "skillId": "id", "mult": "multiplier", "castTime": "cast_time",
            "projectileSpeed": "projectile_speed", "statusEffect": "status_effects",
            "statusEffects": "status_effects", "costType": "cost_type", "damageType": "damage_type",
            "dotDuration": "dot_duration", "hotDuration": "hot_duration", "hitStun": "hitstun",
            "iFrame": "iframe", "comboStep": "combo_step", "aoe_radius": "aoe",
            "targetMode": "target", "target_type": "target", "critAllowed": "crit_allowed",
        }
        for old, new in aliases.items():
            if old in d and new not in d:
                d[new] = d[old]
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        vals = {k: d[k] for k in allowed if k in d}
        vals.setdefault("id", vals.get("name", "skill"))
        for key in ("damage", "multiplier", "cooldown", "cast_time", "recovery", "range",
                    "projectile_speed", "aoe", "cost", "dot", "dot_duration", "hot", "hot_duration",
                    "knockback", "hitstun", "iframe"):
            if key in vals:
                if key == "cost" and isinstance(vals[key], Mapping):
                    costs = vals[key]
                    cost_type = str(vals.get("cost_type", "mana")).lower()
                    vals[key] = _num(costs.get(cost_type, costs.get("mana", next(iter(costs.values()), 0))))
                else:
                    vals[key] = _num(vals[key])
        vals["hits"] = max(1, _int(vals.get("hits", 1), 1))
        vals["combo_step"] = _int(vals.get("combo_step", 0))
        raw_effects = vals.get("status_effects", []) or []
        if isinstance(raw_effects, Mapping):
            raw_effects = [raw_effects]
        vals["status_effects"] = [
            e if isinstance(e, StatusEffect) else StatusEffect.from_dict(e) if isinstance(e, Mapping) else StatusEffect(str(e))
            for e in raw_effects
        ]
        vals["tags"] = [str(x) for x in vals.get("tags", [])]
        vals["target"] = str(vals.get("target", "enemy")).lower()
        vals["crit_allowed"] = _bool(vals.get("crit_allowed", True), True)
        return cls(**vals)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status_effects"] = [asdict(e) for e in self.status_effects]
        return d


@dataclass
class BossPhase:
    id: str
    name: str = ""
    hp_threshold: Optional[float] = None  # fraction (0.5) or absolute HP
    time_threshold: Optional[float] = None
    event: Optional[str] = None
    modifiers: Dict[str, float] = field(default_factory=dict)
    skills: List[str] = field(default_factory=list)
    status_effects: List[StatusEffect] = field(default_factory=list)
    # These compact fields are accepted by the editor/configs in addition to
    # the extensible ``modifiers`` mapping.
    attack_multiplier: float = 1.0
    defense_multiplier: float = 1.0
    once: bool = True
    skill_interval: Optional[float] = None

    def __post_init__(self) -> None:
        self.id = str(self.id)
        self.attack_multiplier = _num(self.attack_multiplier, 1.0)
        self.defense_multiplier = _num(self.defense_multiplier, 1.0)
        if self.skill_interval is not None:
            self.skill_interval = _num(self.skill_interval)
        self.status_effects = [
            effect if isinstance(effect, StatusEffect)
            else StatusEffect.from_dict(effect) if isinstance(effect, Mapping)
            else StatusEffect(str(effect))
            for effect in (self.status_effects or [])
        ]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BossPhase":
        d = dict(data)
        aliases = {
            "hpThreshold": "hp_threshold", "timeThreshold": "time_threshold",
            "add_status": "status_effects", "addStatus": "status_effects",
            "attackMultiplier": "attack_multiplier", "defenseMultiplier": "defense_multiplier",
        }
        for old, new in aliases.items():
            if old in d and new not in d:
                d[new] = d[old]
        # Accept the compact editor form ``trigger: {type, value}`` as well as
        # the normalized threshold fields.  Explicit fields take precedence.
        trigger = d.get("trigger")
        if isinstance(trigger, Mapping):
            trigger_type = str(trigger.get("type", "")).lower()
            trigger_value = trigger.get("value")
            if trigger_type in {"hp", "hp_percent", "health", "health_percent"} and "hp_threshold" not in d:
                numeric_trigger = _num(trigger_value)
                if trigger_type in {"hp_percent", "health_percent"} and abs(numeric_trigger) > 1.0:
                    numeric_trigger /= 100.0
                d["hp_threshold"] = numeric_trigger
            elif trigger_type in {"time", "seconds", "elapsed"} and "time_threshold" not in d:
                d["time_threshold"] = trigger_value
            elif trigger_type in {"event", "on_event"} and "event" not in d:
                d["event"] = trigger_value
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        vals = {k: d[k] for k in allowed if k in d}
        vals.setdefault("id", vals.get("name", "phase"))
        if vals.get("hp_threshold") is not None:
            vals["hp_threshold"] = _num(vals["hp_threshold"])
        if vals.get("time_threshold") is not None:
            vals["time_threshold"] = _num(vals["time_threshold"])
        for key in ("attack_multiplier", "defense_multiplier"):
            if key in vals:
                vals[key] = _num(vals[key], 1.0)
        if vals.get("skill_interval") is not None:
            vals["skill_interval"] = _num(vals["skill_interval"])
        raw_modifiers = vals.get("modifiers", {}) or {}
        vals["modifiers"] = {str(k): _num(v) for k, v in raw_modifiers.items()} if isinstance(raw_modifiers, Mapping) else {}
        vals["skills"] = [str(x) for x in vals.get("skills", [])]
        raw_effects = vals.get("status_effects", []) or []
        if isinstance(raw_effects, Mapping):
            raw_effects = [raw_effects]
        vals["status_effects"] = [
            x if isinstance(x, StatusEffect) else StatusEffect.from_dict(x) if isinstance(x, Mapping) else StatusEffect(str(x))
            for x in raw_effects
        ]
        vals["once"] = _bool(vals.get("once", True), True)
        return cls(**vals)


@dataclass
class Build:
    id: str
    name: str = ""
    stats: Stats = field(default_factory=Stats)
    equipment: List[Dict[str, Any]] = field(default_factory=list)
    skills: List[Skill] = field(default_factory=list)
    passives: List[Dict[str, Any]] = field(default_factory=list)
    bot: str = "aggressive"
    formula: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Build":
        d = dict(data)
        vals: Dict[str, Any] = {}
        vals["id"] = str(d.get("id", d.get("name", "build")))
        vals["name"] = str(d.get("name", vals["id"]))
        # Accept both the canonical nested ``stats`` object and the compact
        # editor shape where HP/attack/etc. live at the build root.
        raw_stats = d.get("stats", d.get("attributes"))
        if raw_stats is None:
            stat_names = {f.name for f in Stats.__dataclass_fields__.values()}
            raw_stats = {k: d[k] for k in stat_names if k in d}
        vals["stats"] = raw_stats
        if not isinstance(vals["stats"], Stats):
            vals["stats"] = Stats.from_dict(vals["stats"])
        raw_skills = d.get("skills", []) or []
        if isinstance(raw_skills, Mapping):
            # Support skills keyed by id.
            raw_skills = [dict(v, id=k) if isinstance(v, Mapping) else {"id": k} for k, v in raw_skills.items()]
        vals["skills"] = [
            x if isinstance(x, Skill) else Skill(id=str(x)) if isinstance(x, str) else Skill.from_dict(x)
            for x in raw_skills
        ]
        vals["equipment"] = copy.deepcopy(d.get("equipment", []) or [])
        vals["passives"] = copy.deepcopy(d.get("passives", []) or [])
        vals["bot"] = str(d.get("bot", d.get("ai_style", "aggressive")))
        vals["formula"] = d.get("formula")
        return cls(**vals)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["stats"] = self.stats.to_dict()
        d["skills"] = [s.to_dict() for s in self.skills]
        return d

    def clone(self, **changes: Any) -> "Build":
        return copy.deepcopy(replace(self, **changes))


@dataclass
class Enemy:
    id: str
    name: str = ""
    stats: Stats = field(default_factory=Stats)
    ai_style: str = "aggressive"
    skills: List[Skill] = field(default_factory=list)
    resistance: Dict[str, float] = field(default_factory=dict)
    phases: List[BossPhase] = field(default_factory=list)
    is_boss: bool = False
    formula: Optional[str] = None
    phase_policy: str = "sequential"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Enemy":
        d = dict(data)
        eid = str(d.get("id", d.get("name", "enemy")))
        raw_stats = d.get("stats", d.get("attributes"))
        if raw_stats is None:
            stat_names = {f.name for f in Stats.__dataclass_fields__.values()}
            raw_stats = {k: d[k] for k in stat_names if k in d}
        stats = raw_stats
        stats = stats if isinstance(stats, Stats) else Stats.from_dict(stats)
        raw_skills = d.get("skills", []) or []
        if isinstance(raw_skills, Mapping):
            raw_skills = [dict(v, id=k) if isinstance(v, Mapping) else {"id": k} for k, v in raw_skills.items()]
        raw_phases = d.get("phases", []) or []
        raw_resistance = d.get("resistance", d.get("resistances", {})) or {}
        if isinstance(raw_resistance, (int, float)):
            raw_resistance = {"physical": raw_resistance}
        return cls(
            id=eid,
            name=str(d.get("name", eid)),
            stats=stats,
            ai_style=str(d.get("ai_style", d.get("aiStyle", "aggressive"))),
            skills=[
                x if isinstance(x, Skill) else Skill(id=str(x)) if isinstance(x, str) else Skill.from_dict(x)
                for x in raw_skills
            ],
            # Use the same signed percentage normalization as ``Stats`` so a
            # config value such as ``20`` means 20% resistance and ``-10``
            # means 10% vulnerability in either representation.
            resistance={str(k): _signed_probability(v) for k, v in raw_resistance.items()},
            phases=[x if isinstance(x, BossPhase) else BossPhase.from_dict(x) for x in raw_phases],
            is_boss=bool(d.get("is_boss", d.get("isBoss", bool(raw_phases)))),
            formula=d.get("formula"),
            phase_policy=str(d.get("phase_policy", d.get("phasePolicy", "sequential"))),
        )

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["stats"] = self.stats.to_dict()
        d["skills"] = [s.to_dict() for s in self.skills]
        return d


@dataclass
class SimulationConfig:
    """Knobs for deterministic, reproducible combat experiments."""

    time_step: float = 0.05
    max_time: float = 120.0
    formula: str = "attack * multiplier + base_damage - defense"
    seed: Optional[int] = None
    record_replay: bool = True
    arena_width: float = 20.0
    starting_distance: float = 2.0
    allow_movement: bool = True
    enable_phases: bool = True
    enable_status_effects: bool = True
    simultaneous: bool = True
    # Arena/action defaults are configurable data rather than hidden balance
    # constants in the resolver.  Aggregate mode uses the exposure factor only
    # because it intentionally omits continuous position traces.
    basic_attack_range: float = 2.0
    engagement_distance: float = 1.5
    dodge_cost: float = 20.0
    dodge_recovery: float = 0.2
    # The data catalogue normally supplies this value.  A full exposure
    # fallback keeps a standalone API caller faithful to the replay path;
    # projects may lower it explicitly when calibrating an aggregate model.
    fast_enemy_exposure: float = 1.0
    # When true, the Formula Lab expression is authoritative even if a
    # combatant carries its own local formula.  This matters for an explicit
    # variant equal to the baseline expression, which cannot be distinguished
    # from the dataclass default by text alone.
    formula_override: bool = False

    def __post_init__(self) -> None:
        self.time_step = max(0.001, float(self.time_step))
        self.max_time = max(0.0, float(self.max_time))
        self.arena_width = max(0.1, float(self.arena_width))
        self.starting_distance = max(0.0, float(self.starting_distance))
        self.basic_attack_range = max(0.1, float(self.basic_attack_range))
        self.engagement_distance = max(0.0, float(self.engagement_distance))
        self.dodge_cost = max(0.0, float(self.dodge_cost))
        self.dodge_recovery = max(0.001, float(self.dodge_recovery))
        self.fast_enemy_exposure = min(1.0, max(0.0, float(self.fast_enemy_exposure)))

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "SimulationConfig":
        d = dict(data or {})
        aliases = {"timeStep": "time_step", "maxTime": "max_time", "recordReplay": "record_replay",
                   "arenaWidth": "arena_width", "startingDistance": "starting_distance",
                   "allowMovement": "allow_movement", "enablePhases": "enable_phases",
                   "enableStatusEffects": "enable_status_effects", "basicAttackRange": "basic_attack_range",
                   "engagementDistance": "engagement_distance", "dodgeCost": "dodge_cost",
                   "dodgeRecovery": "dodge_recovery", "fastEnemyExposure": "fast_enemy_exposure",
                   "formulaOverride": "formula_override"}
        for old, new in aliases.items():
            if old in d and new not in d:
                d[new] = d[old]
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        vals = {k: d[k] for k in allowed if k in d}
        for k in ("time_step", "max_time", "arena_width", "starting_distance", "basic_attack_range",
                  "engagement_distance", "dodge_cost", "dodge_recovery", "fast_enemy_exposure"):
            if k in vals:
                vals[k] = _num(vals[k])
        for k in ("record_replay", "allow_movement", "enable_phases", "enable_status_effects", "simultaneous", "formula_override"):
            if k in vals:
                vals[k] = _bool(vals[k])
        if "formula_override" not in vals:
            # A raw mapping containing ``formula`` is an explicit editor/API
            # choice.  Serialized dataclass mappings carry the field itself,
            # preserving the false default on round-trip.
            vals["formula_override"] = "formula" in d
        if "seed" in vals and vals["seed"] is not None:
            vals["seed"] = _int(vals["seed"])
        return cls(**vals)


@dataclass
class Experiment:
    id: str
    name: str = ""
    research_question: str = ""
    hypothesis: str = ""
    player: Optional[Build] = None
    enemy: Optional[Enemy] = None
    config: SimulationConfig = field(default_factory=SimulationConfig)
    runs: int = 1
    metadata: Dict[str, Any] = field(default_factory=dict)
    hypotheses: List[Dict[str, Any]] = field(default_factory=list)
    results: Dict[str, Any] = field(default_factory=dict)
    conclusion: str = ""
    status: str = "draft"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Experiment":
        d = dict(data)
        exp_id = str(d.get("id", d.get("name", "experiment")))
        raw_config = d.get("config", {})
        config_mapping = raw_config if isinstance(raw_config, Mapping) else {}
        # Experiment records commonly persist the combatants and simulation
        # knobs together under ``config``.  Accept that canonical shape while
        # retaining support for the older top-level fields.
        p = d.get("player", d.get("build", config_mapping.get("player", config_mapping.get("build"))))
        e = d.get("enemy", d.get("boss", config_mapping.get("enemy", config_mapping.get("boss"))))
        simulation_config = config_mapping.get("simulation", config_mapping)
        if not isinstance(simulation_config, Mapping):
            simulation_config = {}
        if isinstance(raw_config, SimulationConfig):
            parsed_config = raw_config
        else:
            parsed_config = SimulationConfig.from_dict(simulation_config)
        return cls(id=exp_id, name=str(d.get("name", exp_id)), research_question=str(d.get("research_question", d.get("researchQuestion", ""))),
                   hypothesis=str(d.get("hypothesis", "")), player=(p if isinstance(p, Build) else Build.from_dict(p) if p else None),
                   enemy=(e if isinstance(e, Enemy) else Enemy.from_dict(e) if e else None),
                   config=parsed_config, runs=max(1, _int(d.get("runs", 1), 1)),
                   metadata=copy.deepcopy(d.get("metadata", {})),
                   hypotheses=copy.deepcopy(d.get("hypotheses", [])),
                   results=copy.deepcopy(d.get("results", {})),
                   conclusion=str(d.get("conclusion", "")),
                   status=str(d.get("status", "draft")))

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["config"] = asdict(self.config)
        if self.player is not None:
            d["player"] = self.player.to_dict()
        if self.enemy is not None:
            d["enemy"] = self.enemy.to_dict()
        return d


def clone_combatant(value: Any) -> Any:
    """Deep-copy a Build/Enemy or plain mapping for sweep isolation."""
    if isinstance(value, (Build, Enemy)):
        return copy.deepcopy(value)
    return copy.deepcopy(value)


__all__ = [
    "Stats", "StatusEffect", "Skill", "BossPhase", "Build", "Enemy",
    "SimulationConfig", "Experiment", "clone_combatant",
]
