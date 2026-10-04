"""Data-driven combat simulation and batch statistics.

This is a small but real event-driven combat loop rather than a calculator
facade.  It advances actors through an arena, schedules cast impacts, resolves
critical hits/dodges/iframes/shields/status effects, changes boss phases, and
records every meaningful transition in :class:`~.events.EventLog`.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
import copy
import math
import random
import re
import statistics
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .bots import Bot, BotDecision, get_bot
from .events import CombatEventType, EventLog
from .formula import FormulaEngine, FormulaError
from .models import BossPhase, Build, Enemy, SimulationConfig, Skill, Stats, StatusEffect


_DEFAULT_FORMULA = "attack * multiplier + base_damage - defense"


def _effective_formula(actor: "_RuntimeActor", cfg: SimulationConfig) -> str:
    """Choose a global Formula Lab override before an actor-local formula.

    The config default remains a fallback so data-driven builds/enemies can
    carry their own formulas.  Any non-default config expression is an
    explicit experiment override (including paired formula variants).
    """
    configured = str(getattr(cfg, "formula", "") or "").strip()
    actor_formula = str(getattr(actor.spec, "formula", "") or "").strip()
    if bool(getattr(cfg, "formula_override", False)) and configured:
        return configured
    if configured and configured.replace(" ", "") not in {
        _DEFAULT_FORMULA.replace(" ", ""),
        "max(0,attack*multiplier+base_damage-defense)",
    }:
        return configured
    return actor_formula or configured or _DEFAULT_FORMULA


def _formula_controls_critical(expression: str) -> bool:
    """Return whether a formula explicitly owns critical-hit scaling.

    The engine supplies the ordinary crit multiplier after evaluating a base
    damage expression.  A formula that references ``is_critical`` or
    ``critical_multiplier`` is self-contained instead, preventing the
    catalogued ``critical_damage`` rule from being multiplied twice.
    """
    return bool(re.search(r"\b(?:is_critical|critical_multiplier)\b", str(expression)))


@dataclass
class _ActiveEffect:
    effect: StatusEffect
    expires_at: float
    next_tick: float
    remaining_stacks: int = 1


@dataclass
class _PendingImpact:
    impact_at: float
    source_id: str
    target_id: str
    skill: Optional[Skill]
    is_basic: bool = False


@dataclass
class _RuntimeActor:
    id: str
    name: str
    is_player: bool
    spec: Build | Enemy
    stats: Stats
    skills: List[Skill]
    bot: Bot
    hp: float
    mana: float
    stamina: float
    position: float
    max_hp: float
    alive: bool = True
    busy_until: float = 0.0
    next_basic_at: float = 0.0
    cooldowns: Dict[str, float] = field(default_factory=dict)
    effects: List[_ActiveEffect] = field(default_factory=list)
    iframe_until: float = 0.0
    hitstun_until: float = 0.0
    shield: float = 0.0
    shield_layers: List[Tuple[float, float, str, str]] = field(default_factory=list, repr=False)
    combo: int = 0
    total_damage: float = 0.0
    damage_taken: float = 0.0
    skill_usage: Counter = field(default_factory=Counter)
    deaths: int = 0
    phase_index: int = 0
    phase_applied: List[str] = field(default_factory=list)
    can_dodge: bool = True
    # Effects change infrequently compared with the number of stat reads in a
    # high-volume simulation.  Cache derived values per actor and invalidate
    # when a buff/debuff/phase is applied or expires.
    stat_cache: Dict[str, float] = field(default_factory=dict, repr=False)
    # Complete declarative skill catalogue; a boss phase can select a subset
    # without permanently losing skills needed by a later phase.
    skill_catalog: List[Skill] = field(default_factory=list, repr=False)
    phase_skill_interval: Optional[float] = field(default=None, repr=False)
    next_phase_skill_at: float = field(default=0.0, repr=False)
    position_updated_at: float = field(default=0.0, repr=False)
    resources_updated_at: float = field(default=0.0, repr=False)


@dataclass
class FightResult:
    """Summary and replay for one simulated fight."""

    winner: str
    win: bool
    duration: float
    ttk: Optional[float]
    player_id: str
    enemy_id: str
    damage_dealt: float
    damage_taken: float
    dps: float
    skill_usage: Dict[str, int]
    deaths: int
    events: List[Dict[str, Any]] = field(default_factory=list)
    seed: Optional[int] = None
    reason: str = ""
    phase_changes: int = 0

    @property
    def replay(self) -> List[Dict[str, Any]]:
        return self.events

    @property
    def player_deaths(self) -> int:
        return int(self.reason == "player_defeated")

    @property
    def enemy_deaths(self) -> int:
        return int(self.reason == "enemy_defeated")

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["player_deaths"] = self.player_deaths
        payload["enemy_deaths"] = self.enemy_deaths
        return payload


@dataclass
class BatchResult:
    """Aggregated statistics from repeated fights."""

    runs: int
    wins: int
    losses: int
    win_rate: float
    average_ttk: Optional[float]
    median_ttk: Optional[float]
    average_dps: float
    average_damage_taken: float
    skill_usage: Dict[str, int]
    deaths: int
    fights: List[FightResult] = field(default_factory=list)
    seed: Optional[int] = None
    simulation_mode: str = "event_replay"
    # Retained even when per-fight traces are dropped for 100/10,000-run
    # batches, so aggregate consumers can still report a real p95 TTK.
    p95_ttk: Optional[float] = None
    # ``deaths`` remains the historical loss/failure count for compatibility;
    # this field separates actual player deaths from time-limit draws.
    player_deaths: int = 0

    @property
    def ttk(self) -> Optional[float]:
        return self.average_ttk

    def to_dict(self, include_fights: bool = False) -> Dict[str, Any]:
        # TTK is only defined for fights in which the target was defeated.
        # Do not mix short player-death durations into the successful-kill
        # distribution: that can make a reported p95 smaller than the median.
        successful_ttks = [f.ttk for f in self.fights if f.ttk is not None]
        if successful_ttks:
            ordered = sorted(successful_ttks)
            p95 = ordered[min(len(ordered) - 1, max(0, math.ceil(len(ordered) * 0.95) - 1))]
        else:
            p95 = self.p95_ttk
        d: Dict[str, Any] = {
            "runs": self.runs,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": self.win_rate,
            "average_ttk": self.average_ttk,
            "median_ttk": self.median_ttk,
            "p95_ttk": p95,
            "average_dps": self.average_dps,
            "average_damage_taken": self.average_damage_taken,
            "skill_usage": dict(self.skill_usage),
            "deaths": self.deaths,
            "player_deaths": self.player_deaths if self.player_deaths else sum(f.player_deaths for f in self.fights),
            "seed": self.seed,
            "simulation_mode": self.simulation_mode,
        }
        d["ttk"] = self.average_ttk
        d["dps"] = self.average_dps
        d["damage_taken"] = self.average_damage_taken
        # Keep a replay-shaped field in the aggregate result so callers can
        # inspect a representative run without having to know the internal
        # ``fights`` storage convention.  The full per-fight traces remain
        # available when ``include_fights=True``.
        if self.fights:
            d["events"] = list(self.fights[0].events)
            d["replay"] = list(self.fights[0].events)
        if include_fights:
            d["fights"] = [f.to_dict() for f in self.fights]
        return d


def _as_build(value: Build | Mapping[str, Any]) -> Build:
    return value if isinstance(value, Build) else Build.from_dict(value)


def _as_enemy(value: Enemy | Build | Mapping[str, Any]) -> Enemy:
    if isinstance(value, Enemy):
        return value
    if isinstance(value, Build):
        return Enemy(id=value.id, name=value.name, stats=value.stats, skills=value.skills, ai_style=value.bot, formula=value.formula)
    return Enemy.from_dict(value)


def _stat_from_mapping(data: Mapping[str, Any], key: str, default: float = 0.0) -> float:
    try:
        return float(data.get(key, default))
    except (TypeError, ValueError):
        return default


class CombatSimulator:
    """Fixed-step deterministic combat simulator.

    The simulator owns no global state.  A seed can be passed per run, making
    batch comparisons reproducible while still allowing independent fights.
    """

    def __init__(self, config: SimulationConfig | Mapping[str, Any] | None = None, formula_engine: FormulaEngine | None = None) -> None:
        self.config = config if isinstance(config, SimulationConfig) else SimulationConfig.from_dict(config or {})
        self.formulas = formula_engine or FormulaEngine()

    def simulate_fight(
        self,
        player: Build | Mapping[str, Any],
        enemy: Enemy | Build | Mapping[str, Any],
        config: SimulationConfig | Mapping[str, Any] | None = None,
        seed: Optional[int] = None,
    ) -> FightResult:
        cfg = config if isinstance(config, SimulationConfig) else SimulationConfig.from_dict(config) if config is not None else copy.deepcopy(self.config)
        p_spec = _as_build(player)
        e_spec = _as_enemy(enemy)
        if seed is None:
            seed = cfg.seed
        rng = random.Random(seed)
        # Aggregate runs can skip transient replay objects; capture-enabled
        # fights still retain the complete event stream for the Replay panel.
        log = EventLog(enabled=bool(cfg.record_replay))
        p = self._runtime_from_build(p_spec, True, -cfg.starting_distance / 2)
        e = self._runtime_from_enemy(e_spec, False, cfg.starting_distance / 2)
        actors = {p.id: p, e.id: e}
        pending: List[_PendingImpact] = []
        now = 0.0
        log.emit(now, CombatEventType.FIGHT_STARTED, p.id, e.id, metadata={"player": p.id, "enemy": e.id})
        # Emit phase one as a visible event when a boss has phases.
        if e_spec.phases and cfg.enable_phases:
            first_phase = e_spec.phases[0]
            if self._phase_triggered(e, first_phase, 0.0, log.seen_types):
                self._apply_phase(e, first_phase, 0.0, log, rng, cfg.enable_status_effects)
        reason = "time_limit"
        if not p.alive or not e.alive:
            reason = "enemy_defeated" if not e.alive and p.alive else "player_defeated" if not p.alive else "draw"
        while now <= cfg.max_time + 1e-9 and p.alive and e.alive:
            # Expirations at a timestamp happen before impacts at that same
            # timestamp, matching ordinary combat-frame ordering.
            self._expire_shields(p, now, log)
            self._expire_shields(e, now, log)
            # Keep event ordering stable: impacts scheduled at this timestamp are
            # resolved before new actions are selected.
            due = [x for x in pending if x.impact_at <= now + 1e-9]
            pending = [x for x in pending if x.impact_at > now + 1e-9]
            for impact in sorted(due, key=lambda x: (x.impact_at, x.source_id, x.target_id)):
                source = actors.get(impact.source_id)
                target = actors.get(impact.target_id)
                if source is None or target is None or not source.alive or not target.alive:
                    continue
                self._resolve_impact(source, target, impact.skill, impact.is_basic, now, log, cfg, rng)
                if not target.alive:
                    reason = "enemy_defeated" if target is e else "player_defeated"
                    break
            if not p.alive or not e.alive:
                break
            # Periodic statuses and resource recovery happen every fixed step.
            for actor in (p, e):
                if actor.alive:
                    self._expire_shields(actor, now, log)
                    self._tick_effects(actor, now, log, cfg, rng, actors)
                    dt = max(0.0, now - actor.resources_updated_at)
                    actor.resources_updated_at = now
                    actor.mana = min(self._stat(actor, "max_mana"), actor.mana + self._stat(actor, "mana_regen") * dt)
                    actor.stamina = min(self._stat(actor, "max_stamina"), actor.stamina + self._stat(actor, "stamina_regen") * dt)
            if not p.alive or not e.alive:
                # A periodic DOT/HOT tick can end a fight outside the pending
                # impact loop.  Preserve the terminal cause for FightResult and
                # the FightEnded replay event instead of leaving ``time_limit``.
                reason = "enemy_defeated" if not e.alive else "player_defeated"
                break
            if cfg.enable_phases and e_spec.phases:
                self._check_phase(e, e_spec.phases, now, log, rng, cfg.enable_status_effects)
            # Decide actions independently; simultaneous=True only changes tie
            # ordering, keeping the loop deterministic.
            for actor, target in ((p, e), (e, p)):
                if not actor.alive or not target.alive:
                    continue
                # Movement is continuous even while an actor is recovering or
                # hit-stunned.  Updating it before the action gate prevents a
                # knockback from permanently desynchronising melee combat.
                self._advance_position(actor, target, cfg, now)
                if now + 1e-9 < actor.busy_until or now + 1e-9 < actor.hitstun_until:
                    continue
                if now + 1e-9 < actor.next_basic_at:
                    continue
                available = self._available_skills(actor, now)
                decision = actor.bot.choose(actor, target, actor.skills, available, rng)
                self._schedule_action(actor, target, decision, now, pending, log, cfg)
            # Jump to the next meaningful event instead of burning CPU on a
            # fixed 20 Hz tick when nothing can happen.  Status/resource
            # updates remain proportional to the elapsed interval, while a
            # small fallback step guarantees progress for zero-duration loops.
            candidates: List[float] = []
            candidates.extend(x.impact_at for x in pending if x.impact_at > now + 1e-9)
            for actor in (p, e):
                for value in (actor.busy_until, actor.next_basic_at, actor.hitstun_until):
                    if value > now + 1e-9:
                        candidates.append(value)
                candidates.extend(a.next_tick for a in actor.effects if a.next_tick > now + 1e-9 and math.isfinite(a.next_tick))
                candidates.extend(a.expires_at for a in actor.effects if a.expires_at > now + 1e-9 and math.isfinite(a.expires_at))
                candidates.extend(layer[0] for layer in actor.shield_layers if layer[0] > now + 1e-9 and math.isfinite(layer[0]))
            if cfg.enable_phases and e_spec.phases:
                candidates.extend(
                    phase.time_threshold
                    for phase in e_spec.phases
                    if phase.id not in e.phase_applied
                    and phase.time_threshold is not None
                    and phase.time_threshold > now + 1e-9
                )
            # A passive actor can leave no scheduled action; use the normal
            # fixed step only in that degenerate case.  Ordinary fights jump
            # directly from cast/impact/cooldown to the next event.
            now = round(min(candidates) if candidates else now + max(0.001, cfg.time_step), 10)
        if p.alive and e.alive:
            reason = "time_limit"
            winner = "draw"
        elif p.alive and not e.alive:
            winner = p.id
        elif e.alive and not p.alive:
            winner = e.id
        else:
            winner = "draw"
        duration = min(now, cfg.max_time)
        ttk = duration if winner == p.id else None
        log.emit(duration, CombatEventType.FIGHT_ENDED, p.id, e.id, metadata={"winner": winner, "reason": reason})
        damage_dealt = p.total_damage
        damage_taken = p.damage_taken
        dps = damage_dealt / duration if duration > 0 else 0.0
        return FightResult(
            winner=winner,
            win=winner == p.id,
            duration=duration,
            ttk=ttk,
            player_id=p.id,
            enemy_id=e.id,
            damage_dealt=damage_dealt,
            damage_taken=damage_taken,
            dps=dps,
            skill_usage=dict(p.skill_usage),
            deaths=p.deaths + e.deaths,
            events=log.to_list() if cfg.record_replay else [],
            seed=seed,
            reason=reason,
            phase_changes=max(0, e.phase_index),
        )

    # Friendly aliases used by scripts and UI adapters.
    simulate = simulate_fight
    run_fight = simulate_fight

    def run_batch(
        self,
        player: Build | Mapping[str, Any],
        enemy: Enemy | Build | Mapping[str, Any],
        runs: int = 100,
        config: SimulationConfig | Mapping[str, Any] | None = None,
        seed: Optional[int] = None,
        keep_fights: bool = True,
    ) -> BatchResult:
        runs = max(1, int(runs))
        effective_cfg = config if isinstance(config, SimulationConfig) else SimulationConfig.from_dict(config) if config is not None else self.config
        base_seed = seed if seed is not None else effective_cfg.seed
        # Large aggregate studies do not need a full replay for every sample.
        # Use the accelerated event sampler below while retaining the exact
        # event-driven path for one-fight/replay work and all small tests.
        use_fast = runs >= 100 and not effective_cfg.record_replay
        fights: List[FightResult] = []
        for i in range(runs):
            fight_seed = (base_seed + i) if base_seed is not None else None
            if use_fast:
                p_spec = _as_build(player)
                e_spec = _as_enemy(enemy)
                fights.append(self._simulate_fast(p_spec, e_spec, effective_cfg, fight_seed))
            else:
                fights.append(self.simulate_fight(player, enemy, config=config, seed=fight_seed))
        wins = sum(1 for f in fights if f.win)
        ttks = [f.ttk for f in fights if f.ttk is not None]
        usages: Counter = Counter()
        for f in fights:
            usages.update(f.skill_usage)
        # Keep p95 on the same population as mean/median TTK: successful
        # kills only. A batch with no wins has no TTK percentile and reports
        # ``null`` instead of a misleading time-limit/death duration.
        successful_ttks = sorted(float(f.ttk) for f in fights if f.ttk is not None)
        p95_ttk = successful_ttks[min(len(successful_ttks) - 1, max(0, math.ceil(len(successful_ttks) * 0.95) - 1))] if successful_ttks else None
        actual_player_deaths = sum(f.player_deaths for f in fights)
        return BatchResult(
            runs=runs,
            wins=wins,
            losses=runs - wins,
            win_rate=wins / runs,
            average_ttk=statistics.mean(ttks) if ttks else None,
            median_ttk=statistics.median(ttks) if ttks else None,
            average_dps=statistics.mean([f.dps for f in fights]) if fights else 0.0,
            average_damage_taken=statistics.mean([f.damage_taken for f in fights]) if fights else 0.0,
            skill_usage=dict(usages),
            # For balance reports ``deaths`` means player deaths, not the
            # defeated target's terminal death event.  A draw/time-out counts
            # as a player failure as well.
            deaths=sum(1 for f in fights if not f.win),
            fights=fights if keep_fights else [],
            seed=base_seed,
            simulation_mode="accelerated_approximation" if use_fast else "event_replay",
            p95_ttk=p95_ttk,
            player_deaths=actual_player_deaths,
        )

    def _simulate_fast(self, player: Build, enemy: Enemy, cfg: SimulationConfig, seed: Optional[int]) -> FightResult:
        """Accelerated aggregate sampler for high-volume, no-replay runs.

        It is still a combat simulation: cooldowns, resources, crit/dodge,
        resistance, shields, DOT/HOT, hitstun, boss phases, and the configured
        damage formula are resolved on an event clock.  It omits allocation of
        the full replay object; callers can request one representative full
        replay separately (as the demo runner does).
        """
        rng = random.Random(seed)
        p = self._runtime_from_build(player, True, -cfg.starting_distance / 2)
        e = self._runtime_from_enemy(enemy, False, cfg.starting_distance / 2)
        actors = {p.id: p, e.id: e}
        silent_log = EventLog(enabled=False)
        next_time = {p.id: 0.0, e.id: 0.0}
        phase_seen: set[str] = set()
        event_markers: set[str] = set()
        # Self-targeted effects are delayed until their cast time, matching
        # the pending-impact queue used by the replay path.  Keeping this
        # small queue avoids granting shields/heals/buffs at cast start in
        # accelerated batches.
        pending_self_effects: list[tuple[float, _RuntimeActor, list[StatusEffect]]] = []
        now = 0.0
        reason = "time_limit"
        if not p.alive or not e.alive:
            reason = "enemy_defeated" if not e.alive and p.alive else "player_defeated" if not p.alive else "draw"

        def apply_phase_fast() -> None:
            if not cfg.enable_phases or not enemy.phases:
                return
            policy = str(getattr(enemy, "phase_policy", "sequential")).lower()
            sequential = policy not in {"independent", "all"}
            first_matching = policy in {"first_matching_threshold", "first_matching"}
            for phase in enemy.phases:
                triggered = self._phase_triggered(e, phase, now, event_markers)
                if phase.id in phase_seen:
                    continue
                # Boss phases are ordered.  A later HP/event condition cannot
                # skip an earlier phase that is still waiting for its trigger.
                if triggered:
                    phase_seen.add(phase.id)
                    self._apply_phase(e, phase, now, silent_log, rng, cfg.enable_status_effects)
                    if first_matching:
                        break
                    continue
                if sequential:
                    break

        while now <= cfg.max_time + 1e-9 and p.alive and e.alive:
            actor = p if next_time[p.id] <= next_time[e.id] else e
            target = e if actor is p else p
            current_time = now
            scheduled_time = max(now, next_time[actor.id])
            if pending_self_effects:
                next_effect_time = min(item[0] for item in pending_self_effects)
                scheduled_time = min(scheduled_time, next_effect_time)
            phase_clock = min(
                (phase.time_threshold for phase in enemy.phases
                 if phase.id not in phase_seen and phase.time_threshold is not None
                 and current_time + 1e-9 < phase.time_threshold <= scheduled_time + 1e-9),
                default=None,
            ) if cfg.enable_phases else None
            phase_blocked_action = phase_clock is not None
            next_clock = phase_clock if phase_blocked_action else scheduled_time
            # Do not execute an action that was scheduled after the fight
            # window.  The loop condition is checked before ``now`` is
            # advanced, so an unchecked jump here would count out-of-window
            # damage while still reporting a clipped duration.
            if next_clock > cfg.max_time + 1e-9:
                now = cfg.max_time
                break
            now = next_clock
            self._advance_position(actor, target, cfg, now)
            for resource_actor in actors.values():
                dt = max(0.0, now - getattr(resource_actor, "_fast_last", 0.0))
                self._expire_shields(resource_actor, now, silent_log)
                if cfg.enable_status_effects and resource_actor.alive:
                    self._tick_effects(resource_actor, now, silent_log, cfg, rng, actors)
                resource_actor.mana = min(self._stat(resource_actor, "max_mana"), resource_actor.mana + self._stat(resource_actor, "mana_regen") * dt)
                resource_actor.stamina = min(self._stat(resource_actor, "max_stamina"), resource_actor.stamina + self._stat(resource_actor, "stamina_regen") * dt)
                setattr(resource_actor, "_fast_last", now)
            if cfg.enable_status_effects and pending_self_effects:
                ready: list[tuple[float, _RuntimeActor, list[StatusEffect]]] = []
                waiting: list[tuple[float, _RuntimeActor, list[StatusEffect]]] = []
                for due_time, caster, effects_due in pending_self_effects:
                    (ready if due_time <= now + 1e-9 else waiting).append((due_time, caster, effects_due))
                pending_self_effects = waiting
                for _due_time, caster, effects_due in sorted(ready, key=lambda row: row[0]):
                    if not caster.alive:
                        continue
                    for effect in effects_due:
                        effect = copy.deepcopy(effect)
                        effect.source_id = caster.id
                        self._apply_effect(caster, effect, now, silent_log, rng)
            if not p.alive or not e.alive:
                reason = "enemy_defeated" if not e.alive else "player_defeated"
                break
            # Event-triggered phases use the same canonical names as replay
            # events.  The disabled log still tracks seen types; mirror it into
            # the lightweight marker set used by the aggregate loop.
            event_markers.update(silent_log.seen_types)
            apply_phase_fast()
            if phase_blocked_action or now + 1e-9 < next_time[actor.id]:
                continue
            available = self._available_skills(actor, now)
            decision = actor.bot.choose(actor, target, actor.skills, available, rng)
            if decision.action == "wait":
                next_time[actor.id] = now + max(cfg.time_step, 0.05)
                continue
            if decision.action == "dodge" and actor.stamina + 1e-9 < cfg.dodge_cost:
                decision = BotDecision("basic_attack")
            if decision.action == "dodge":
                event_markers.add("dodge")
                actor.stamina = max(0.0, actor.stamina - cfg.dodge_cost)
                actor.iframe_until = now + max(0.0, self._stat(actor, "iframe"))
                actor.combo = 0
                next_time[actor.id] = now + max(cfg.time_step, cfg.dodge_recovery)
                continue
            skill = decision.skill if decision.action == "skill" else None
            if skill is not None:
                resource_name = "mana" if skill.cost_type.lower() == "mana" else "stamina"
                if getattr(actor, resource_name) + 1e-9 < skill.cost:
                    skill = None
                else:
                    setattr(actor, resource_name, getattr(actor, resource_name) - skill.cost)
            if skill is None:
                event_markers.add("attackstarted")
                multiplier, base_damage = 1.0, 0.0
                cooldown, cast_recovery = max(cfg.time_step, 1.0 / max(0.05, self._stat(actor, "attack_speed"))), 0.0
                damage_type, hits = "physical", 1
                effects: Sequence[StatusEffect] = ()
                hitstun = self._stat(actor, "hitstun")
                knockback = self._stat(actor, "knockback")
            else:
                event_markers.add("skillcast")
                multiplier, base_damage = skill.multiplier, skill.damage
                cdr = min(0.95, max(0.0, self._stat(actor, "cooldown_reduction")))
                cooldown, cast_recovery = max(0.001, cfg.time_step, skill.cooldown * (1.0 - cdr)), max(cfg.time_step, skill.cast_time + skill.recovery)
                damage_type, hits = skill.damage_type, max(1, skill.hits)
                effects = list(skill.status_effects)
                if skill.dot > 0 and skill.dot_duration > 0:
                    effects.append(StatusEffect(f"{skill.id}:dot", f"{skill.name} DOT", "dot", skill.dot_duration, skill.dot, 1.0, damage_type=skill.damage_type, source_id=actor.id))
                if skill.hot > 0 and skill.hot_duration > 0:
                    effects.append(StatusEffect(f"{skill.id}:hot", f"{skill.name} HOT", "hot", skill.hot_duration, skill.hot, 1.0, source_id=actor.id))
                hitstun = skill.hitstun or self._stat(actor, "hitstun")
                knockback = skill.knockback or self._stat(actor, "knockback")
                actor.skill_usage[skill.id] += 1
                actor.cooldowns[skill.id] = now + cooldown
                if skill.iframe > 0:
                    actor.iframe_until = max(actor.iframe_until, now + skill.iframe)
                if not actor.is_player and actor.phase_skill_interval is not None:
                    actor.next_phase_skill_at = now + max(0.0, actor.phase_skill_interval)
            # Defensive/self-targeted skills resolve their effects on the
            # caster.  They are still scheduled on the same event clock so
            # cast/recovery timing remains part of the simulation.
            if skill is not None and skill.target in {"self", "caster", "ally"}:
                if cfg.enable_status_effects:
                    due_time = now + max(cfg.time_step, skill.cast_time)
                    pending_self_effects.append((due_time, actor, [copy.deepcopy(effect) for effect in effects]))
                next_time[actor.id] = max(now + max(0.05, cast_recovery), actor.hitstun_until)
                continue
            attack_range = (skill.range + max(0.0, skill.aoe)) if skill is not None else cfg.basic_attack_range
            if not self._in_range(actor, target, attack_range, cfg):
                # Match the regular arena's range gate.  The fast path does not
                # retain a position trace, but it still advances movement and
                # refuses to award damage while a target is out of reach.
                event_markers.add("miss")
                next_time[actor.id] = now + max(0.05, cast_recovery)
                continue
            if now < target.iframe_until or rng.random() < min(0.95, max(0.0, self._stat(target, "dodge"))):
                event_markers.add("dodge")
                # A missed/iframes-blocked impact breaks the source's hit
                # streak just like the regular event-clock resolver.
                actor.combo = 0
                next_time[actor.id] = now + max(0.05, cast_recovery)
                continue
            # The aggregate path resolves the hit at the action clock for
            # throughput, but it must still respect the combat window.  A
            # cast/projectile whose impact would land after ``max_time`` is a
            # draw, just like the replay path's pending-impact queue.
            travel = 0.0
            if skill is not None and skill.projectile_speed > 0:
                travel = abs(actor.position - target.position) / skill.projectile_speed
            impact_delay = (max(cfg.time_step, skill.cast_time) + travel) if skill is not None else max(0.001, cfg.time_step)
            if now + impact_delay > cfg.max_time + 1e-9:
                next_time[actor.id] = now + max(0.05, cast_recovery + travel)
                continue
            event_markers.add("hit")
            for hit_index in range(hits):
                critical = bool(skill is None or skill.crit_allowed) and rng.random() < min(1.0, max(0.0, self._stat(actor, "critical")))
                if critical:
                    event_markers.add("critical")
                attack_value = self._stat(actor, "attack")
                defense_value = self._stat(target, "defense")
                damage_below_half_hp = self._conditional_damage_bonus(actor, target)
                expression = _effective_formula(actor, cfg)
                if expression.replace(" ", "") in {
                    "attack*multiplier+base_damage-defense", "max(0,attack*multiplier+base_damage-defense)",
                    "attack*multiplier-defense", "max(0,attack*multiplier-defense)",
                }:
                    raw = max(0.0, attack_value * multiplier + base_damage - defense_value)
                else:
                    try:
                        raw = self.formulas.evaluate(expression, {
                            "attack": attack_value,
                            "defense": defense_value,
                            "multiplier": multiplier,
                            "base_damage": base_damage,
                            "critical_multiplier": self._stat(actor, "crit_multiplier"),
                            "critical": self._stat(actor, "critical"),
                            "is_critical": int(critical),
                            "target_hp": target.hp,
                            "target_max_hp": target.max_hp,
                            "source_hp": actor.hp,
                            "source_max_hp": actor.max_hp,
                            "combo": actor.combo,
                            "stacks": 1,
                            "damage_below_half_hp": damage_below_half_hp,
                            "distance": abs(actor.position - target.position),
                            "resistance": target.stats.resistances.get(damage_type, 0.0),
                            "damage_taken": actor.damage_taken,
                            "hit_count": hit_index + 1,
                            "time": now,
                        })
                    except FormulaError:
                        raw = max(0.0, attack_value * multiplier + base_damage - defense_value)
                # Apply the declarative conditional multiplier after formula
                # evaluation (and before crit/resistance), matching regular
                # replay semantics while still exposing the active bonus to
                # custom formulas.
                raw *= max(0.0, 1.0 + damage_below_half_hp)
                if critical and not _formula_controls_critical(expression):
                    raw *= max(0.0, self._stat(actor, "crit_multiplier"))
                # The fast aggregate path does not materialise continuous
                # positions, so apply a calibrated exposure factor to enemy
                # outgoing hits.  Full replay fights use exact range/knockback
                # resolution above; this keeps aggregate survival comparable
                # without pretending omitted position traces were observed.
                if not actor.is_player:
                    raw *= cfg.fast_enemy_exposure
                resistance = target.stats.resistances.get(damage_type, 0.0)
                amount = max(0.0, raw * max(0.0, 1.0 - resistance))
                absorbed = self._consume_shield(target, amount)
                damage = amount - absorbed
                target.hp = max(0.0, target.hp - damage)
                actor.total_damage += damage
                target.damage_taken += damage
                event_markers.add("damage")
                if damage > 0:
                    actor.combo += max(1, skill.combo_step if skill is not None and skill.combo_step else 1)
                if target.hp <= 0:
                    target.alive = False; target.deaths += 1; event_markers.add("death"); reason = "enemy_defeated" if target is e else "player_defeated"; break
            if cfg.enable_status_effects and target.alive:
                for effect in effects:
                    effect = copy.deepcopy(effect)
                    effect.source_id = actor.id
                    self._apply_effect(target, effect, now, silent_log, rng)
                if not target.alive or target.hp <= 0:
                    target.alive = False
                    if target.deaths <= 0:
                        target.deaths += 1
                    event_markers.add("death")
                    reason = "enemy_defeated" if target is e else "player_defeated"
            if target.alive and knockback > 0:
                resistance = min(1.0, max(0.0, self._stat(target, "knockback_resistance")))
                distance = max(0.0, knockback) * (1.0 - resistance)
                direction = 1.0 if target.position >= actor.position else -1.0
                target.position = max(-cfg.arena_width / 2, min(cfg.arena_width / 2, target.position + direction * distance))
            # Recovery gates the next decision; cooldown gates this skill in
            # ``actor.cooldowns``.  Adding both would incorrectly prevent a
            # rotation from selecting another ready skill.
            # Basic attacks use their attack-speed interval as the action
            # gate.  Skill cooldowns remain in ``actor.cooldowns`` so a bot
            # can rotate through other ready skills during a long cooldown;
            # only the cast/recovery time gates the next decision for skills.
            action_delay = cooldown if skill is None else cast_recovery
            next_time[actor.id] = now + max(0.05, action_delay + travel)
            if hitstun > 0:
                resistance = min(1.0, max(0.0, self._stat(target, "hitstun_resistance")))
                next_time[target.id] = max(next_time[target.id], now + hitstun * (1.0 - resistance))
            if cfg.enable_status_effects and target.alive:
                next_time[target.id] = max(next_time[target.id], target.hitstun_until)
        winner = p.id if p.alive and not e.alive else e.id if e.alive and not p.alive else "draw"
        duration = min(now, cfg.max_time)
        # ``phase_changes`` counts transitions after the initial phase, which
        # matches the replay path's zero-based phase index.
        return FightResult(winner, winner == p.id, duration, duration if winner == p.id else None, p.id, e.id, p.total_damage, p.damage_taken, p.total_damage / max(0.001, duration), dict(p.skill_usage), p.deaths + e.deaths, [], seed, reason, max(0, len(phase_seen) - 1))

    batch = run_batch

    def _runtime_from_build(self, spec: Build, is_player: bool, position: float) -> _RuntimeActor:
        stats = self._merged_stats(spec.stats, spec.equipment, spec.passives)
        # Skills are immutable during a fight.  Effects are copied when they
        # are applied, so cloning the complete skill graph for every aggregate
        # sample only adds substantial deepcopy overhead (especially at 10k
        # runs) without providing isolation.  Keep a private list container so
        # runtime code can never append/remove from the Build's catalogue.
        return _RuntimeActor(spec.id, spec.name or spec.id, is_player, spec, stats, list(spec.skills), get_bot(spec.bot),
                             stats.hp, stats.mana, stats.stamina, position, stats.max_hp or stats.hp,
                             alive=stats.hp > 0.0,
                             deaths=1 if stats.hp <= 0.0 else 0,
                             skill_catalog=list(spec.skills))

    def _runtime_from_enemy(self, spec: Enemy, is_player: bool, position: float) -> _RuntimeActor:
        stats = self._merged_stats(spec.stats, [], [])
        # Enemy-level resistance augments stat resistance.
        # Enemy-level resistance uses the same signed percentage convention as
        # ``Stats.resistances``.  ``Enemy.from_dict`` already normalizes values,
        # but direct ``Enemy(...)`` construction may still provide percentages.
        for key, value in spec.resistance.items():
            numeric = float(value)
            if abs(numeric) > 1.0:
                numeric /= 100.0
            stats.resistances[str(key)] = min(1.0, max(-1.0, numeric))
        return _RuntimeActor(spec.id, spec.name or spec.id, is_player, spec, stats, list(spec.skills), get_bot(spec.ai_style),
                             stats.hp, stats.mana, stats.stamina, position, stats.max_hp or stats.hp,
                             alive=stats.hp > 0.0,
                             deaths=1 if stats.hp <= 0.0 else 0,
                             skill_catalog=list(spec.skills))

    @staticmethod
    def _merged_stats(base: Stats, equipment: Sequence[Mapping[str, Any]], passives: Sequence[Mapping[str, Any]]) -> Stats:
        stats = copy.deepcopy(base)
        aliases = {
            "critical_chance": "critical", "crit": "critical", "crit_chance": "critical",
            "dodge_chance": "dodge", "i_frame": "iframe", "iframe_duration": "iframe",
            "attackSpeed": "attack_speed", "moveSpeed": "move_speed",
            "cooldownReduction": "cooldown_reduction", "critDamage": "crit_multiplier",
            "criticalChanceMultiplier": "critical_chance_multiplier",
            "critChanceMultiplier": "critical_chance_multiplier",
            "hitstun_bonus": "hitstun", "hitStunBonus": "hitstun",
            "stagger_resistance": "hitstun_resistance", "staggerResistance": "hitstun_resistance",
            "knockbackResistance": "knockback_resistance", "shieldReceived": "shield_received",
        }
        resource_deltas = {"hp": 0.0, "mana": 0.0, "stamina": 0.0}
        max_resource_deltas = {"max_hp": 0.0, "max_mana": 0.0, "max_stamina": 0.0}
        # Equipment/passives may use declarative multiplicative modifiers such
        # as ``attack_multiplier: 1.2``.  Keep these separate from flat deltas
        # and apply them once after all item records have been merged.  The
        # ``critical_multiplier`` is a legacy/data-catalogue flat bonus to the
        # critical-damage stat (see the bundled Executioner passive).  Use the
        # explicit ``critical_chance_multiplier`` spelling when a loadout wants
        # to multiply crit chance instead.
        stat_multipliers: Dict[str, float] = {}
        stat_fields = {f.name for f in Stats.__dataclass_fields__.values()} - {"resistances"}
        for item in list(equipment) + list(passives):
            # A record may expose both baseline stat deltas and conditional
            # effects.  Merge all declared sections instead of selecting the
            # first one, otherwise an item such as
            # ``{stats: {...}, effects: {damage_below_half_hp: 0.12}}`` would
            # silently lose its passive rule.
            if isinstance(item, Mapping):
                sections = [item.get(name) for name in ("stats", "modifiers", "effects")
                            if isinstance(item.get(name), Mapping)]
                if sections:
                    modifiers: Mapping[str, Any] = {}
                    merged_sections: Dict[str, Any] = {}
                    for section in sections:
                        merged_sections.update(section)
                    modifiers = merged_sections
                else:
                    modifiers = item
            else:
                modifiers = {}
            for key, value in modifiers.items():
                key = aliases.get(str(key), str(key))
                try:
                    numeric_value = float(value)
                except (TypeError, ValueError):
                    numeric_value = 0.0
                legacy_crit_damage = key == "critical_multiplier"
                if legacy_crit_damage:
                    key = "crit_multiplier"
                if key.endswith("_multiplier") and not legacy_crit_damage:
                    multiplier_name = key[:-len("_multiplier")]
                    multiplier_name = aliases.get(multiplier_name, multiplier_name)
                    if multiplier_name in stat_fields:
                        stat_multipliers[multiplier_name] = stat_multipliers.get(multiplier_name, 1.0) * numeric_value
                        continue
                if key in resource_deltas:
                    resource_deltas[key] += numeric_value
                if key in max_resource_deltas:
                    max_resource_deltas[key] += numeric_value
                if hasattr(stats, key) and key not in {"resistances", "max_hp", "max_mana", "max_stamina"}:
                    try:
                        setattr(stats, key, getattr(stats, key) + numeric_value)
                    except (TypeError, ValueError):
                        pass
            if isinstance(modifiers, Mapping) and isinstance(modifiers.get("resistances"), Mapping):
                stats.resistances.update({k: float(v) for k, v in modifiers["resistances"].items()})
        # A flat ``hp``/``mana``/``stamina`` modifier represents additional
        # capacity as well as the starting resource.  Explicit max_* modifiers
        # remain additive and are not double-counted.
        for resource, delta in resource_deltas.items():
            max_name = f"max_{resource}"
            if delta and max_resource_deltas[max_name] == 0:
                setattr(stats, max_name, (getattr(stats, max_name) or 0.0) + delta)
        for name, delta in max_resource_deltas.items():
            if delta:
                setattr(stats, name, (getattr(stats, name) or 0.0) + delta)
        for name, multiplier in stat_multipliers.items():
            try:
                setattr(stats, name, getattr(stats, name) * multiplier)
            except (AttributeError, TypeError, ValueError):
                pass
        stats.__post_init__()
        return stats

    def _stat(self, actor: _RuntimeActor, name: str) -> float:
        cached = actor.stat_cache.get(name)
        if cached is not None:
            return cached
        value = float(getattr(actor.stats, name, 0.0))
        multiplier = 1.0
        # Status data is authored by humans and may use the same compact
        # aliases accepted by the build editor.  Resolve those aliases at the
        # active-effect boundary too, so a buff such as
        # ``critical_chance_multiplier`` actually changes the canonical
        # ``Stats.critical`` value instead of being silently ignored.
        flat_aliases = {
            "critical": ("critical", "critical_chance", "crit", "crit_chance"),
            "crit_multiplier": ("crit_multiplier", "critical_damage", "critDamage"),
            "attack_speed": ("attack_speed", "attackSpeed"),
            "move_speed": ("move_speed", "moveSpeed"),
            "cooldown_reduction": ("cooldown_reduction", "cooldownReduction"),
            "iframe": ("iframe", "i_frame", "iframe_duration"),
            "dodge": ("dodge", "dodge_chance"),
            "knockback_resistance": ("knockback_resistance", "knockbackResistance"),
            "hitstun_resistance": ("hitstun_resistance", "stagger_resistance", "staggerResistance"),
            "shield_received": ("shield_received", "shieldReceived"),
        }
        flat_keys = flat_aliases.get(name, (name,))
        multiplier_keys = [f"{key}_multiplier" for key in flat_keys]
        if name == "critical":
            multiplier_keys.extend(("critical_chance_multiplier", "crit_chance_multiplier",
                                    "criticalChanceMultiplier", "critChanceMultiplier"))
        elif name == "attack_speed":
            multiplier_keys.append("attackSpeedMultiplier")
        elif name == "move_speed":
            multiplier_keys.append("moveSpeedMultiplier")
        elif name == "cooldown_reduction":
            multiplier_keys.append("cooldownReductionMultiplier")
        for active in actor.effects:
            stacks = max(1, active.remaining_stacks)
            flat_value = next((active.effect.modifiers[key] for key in flat_keys if key in active.effect.modifiers), 0.0)
            value += float(flat_value) * stacks
            # Config authors may express a modifier as either a flat delta
            # (``attack: 10``) or a multiplier (``attack_multiplier: 1.2``).
            # Supporting both here keeps phase and status data declarative and
            # avoids accidentally treating 1.2 as a +120% flat stat bonus.
            raw_multiplier = next((active.effect.modifiers[key] for key in multiplier_keys if key in active.effect.modifiers), None)
            if raw_multiplier is not None:
                try:
                    multiplier *= max(0.0, float(raw_multiplier)) ** stacks
                except (TypeError, ValueError):
                    pass
        value *= multiplier
        actor.stat_cache[name] = value
        return value

    def _conditional_damage_bonus(self, source: _RuntimeActor, target: _RuntimeActor) -> float:
        """Return the active execution-style damage bonus for one hit.

        ``damage_below_half_hp`` is a declarative additive multiplier (for
        example ``0.12`` means +12%).  Keep the threshold check in one helper
        so the regular replay and accelerated aggregate paths cannot drift.
        The bonus is evaluated immediately before each hit, which is important
        for multi-hit skills that cross the 50% boundary mid-cast.
        """
        try:
            bonus = float(self._stat(source, "damage_below_half_hp"))
        except (TypeError, ValueError):
            return 0.0
        if not math.isfinite(bonus) or target.max_hp <= 0 or target.hp >= target.max_hp * 0.5:
            return 0.0
        # A negative value is allowed as a data-driven penalty, but may not
        # invert damage.  Positive values remain unconstrained so balance
        # experiments can intentionally sweep large execution bonuses.
        return max(-1.0, bonus)

    @staticmethod
    def _invalidate_stats(actor: _RuntimeActor) -> None:
        actor.stat_cache.clear()

    def _available_skills(self, actor: _RuntimeActor, now: float) -> List[Skill]:
        result = []
        if not actor.is_player and actor.phase_skill_interval is not None and now + 1e-9 < actor.next_phase_skill_at:
            return result
        cdr = min(0.95, max(0.0, self._stat(actor, "cooldown_reduction")))
        for skill in actor.skills:
            if now < actor.cooldowns.get(skill.id, 0.0):
                continue
            resource = actor.mana if skill.cost_type.lower() == "mana" else actor.stamina
            if resource + 1e-9 < skill.cost:
                continue
            # Zero/negative cooldown is legal as a stress test, but one action
            # per simulation tick prevents an accidental infinite loop.
            result.append(skill)
        return result

    def _schedule_action(self, actor: _RuntimeActor, target: _RuntimeActor, decision: BotDecision, now: float,
                         pending: List[_PendingImpact], log: EventLog, cfg: SimulationConfig) -> None:
        if decision.action == "wait":
            actor.busy_until = now + max(cfg.time_step, 0.001)
            actor.next_basic_at = actor.busy_until
            return
        if decision.action == "dodge" and actor.stamina + 1e-9 < cfg.dodge_cost:
            decision = BotDecision("basic_attack")
        if decision.action == "dodge":
            cost = min(actor.stamina, cfg.dodge_cost)
            actor.stamina -= cost
            actor.iframe_until = max(actor.iframe_until, now + max(0.0, self._stat(actor, "iframe")))
            actor.combo = 0
            actor.next_basic_at = now + max(0.001, cfg.time_step, cfg.dodge_recovery)
            log.emit(now, CombatEventType.DODGE, actor.id, target.id, amount=0.0, message="evade window")
            return
        if decision.action == "skill" and decision.skill is not None:
            skill = decision.skill
            resource_name = "mana" if skill.cost_type.lower() == "mana" else "stamina"
            current = getattr(actor, resource_name)
            if current + 1e-9 < skill.cost:
                return
            setattr(actor, resource_name, current - skill.cost)
            cdr = min(0.95, max(0.0, self._stat(actor, "cooldown_reduction")))
            # Even a deliberately zero-cooldown/zero-cast stress skill gets
            # one simulation frame.  This prevents a malformed config from
            # creating an infinite same-timestamp loop while preserving the
            # cooldown-loop warning for balance analysis.
            cooldown = max(cfg.time_step, skill.cooldown * (1.0 - cdr))
            actor.cooldowns[skill.id] = now + cooldown
            actor.skill_usage[skill.id] += 1
            if not actor.is_player and actor.phase_skill_interval is not None:
                actor.next_phase_skill_at = now + max(0.0, actor.phase_skill_interval)
            actor.busy_until = now + max(cfg.time_step, skill.cast_time + skill.recovery)
            actor.next_basic_at = actor.busy_until
            if skill.iframe > 0:
                actor.iframe_until = max(actor.iframe_until, now + skill.iframe)
            cast_target = actor if skill.target in {"self", "caster", "ally"} else target
            log.emit(now, CombatEventType.SKILL_CAST, actor.id, cast_target.id, skill_id=skill.id,
                     metadata={"cost": skill.cost, "cooldown": cooldown, "target": skill.target,
                               "projectile_speed": skill.projectile_speed, "aoe_radius": skill.aoe})
            impact_target = cast_target
            if impact_target is actor:
                pending.append(_PendingImpact(now + max(cfg.time_step, skill.cast_time), actor.id, actor.id, skill, False))
            elif self._in_range(actor, target, skill.range + max(0.0, skill.aoe), cfg):
                travel = 0.0
                if skill.projectile_speed > 0:
                    travel = abs(actor.position - target.position) / skill.projectile_speed
                pending.append(_PendingImpact(now + max(cfg.time_step, skill.cast_time) + travel, actor.id, target.id, skill, False))
            else:
                # Projectile/range travel is modeled as movement; emit a miss
                # marker in replay while preserving the cast event.
                log.emit(now, CombatEventType.HIT, actor.id, target.id, skill_id=skill.id, metadata={"result": "out_of_range"})
            return
        # Basic attack
        interval = max(cfg.time_step, 1.0 / max(0.05, self._stat(actor, "attack_speed")))
        actor.next_basic_at = now + interval
        actor.busy_until = now
        log.emit(now, CombatEventType.ATTACK_STARTED, actor.id, target.id)
        if self._in_range(actor, target, cfg.basic_attack_range, cfg):
            pending.append(_PendingImpact(now + max(0.001, cfg.time_step), actor.id, target.id, None, True))

    @staticmethod
    def _in_range(actor: _RuntimeActor, target: _RuntimeActor, attack_range: float, cfg: SimulationConfig) -> bool:
        return abs(actor.position - target.position) <= max(0.1, attack_range)

    def _advance_position(self, actor: _RuntimeActor, target: _RuntimeActor, cfg: SimulationConfig, now: float | None = None) -> None:
        if not cfg.allow_movement:
            if now is not None:
                actor.position_updated_at = now
            return
        distance = target.position - actor.position
        desired = cfg.engagement_distance
        if abs(distance) <= desired:
            if now is not None:
                actor.position_updated_at = now
            return
        if now is None:
            elapsed = cfg.time_step
        else:
            elapsed = max(0.0, now - actor.position_updated_at)
            actor.position_updated_at = now
            # Preserve the original first-frame movement convention without
            # charging a full step repeatedly at the same timestamp.
            if elapsed <= 0.0 and now <= 0.0:
                elapsed = cfg.time_step
        step = max(0.0, self._stat(actor, "move_speed")) * elapsed
        actor.position += math.copysign(min(abs(distance) - desired, step), distance)
        actor.position = max(-cfg.arena_width / 2, min(cfg.arena_width / 2, actor.position))

    def _resolve_impact(self, source: _RuntimeActor, target: _RuntimeActor, skill: Optional[Skill], is_basic: bool,
                        now: float, log: EventLog, cfg: SimulationConfig, rng: random.Random) -> None:
        if skill is not None and skill.target in {"self", "caster", "ally"} and target is source:
            # Utility skills have no hostile hit resolution.  Their effects are
            # applied after cast time, with the caster as the event target.
            if cfg.enable_status_effects:
                effects = list(skill.status_effects)
                if skill.dot > 0 and skill.dot_duration > 0:
                    effects.append(StatusEffect(f"{skill.id}:dot", f"{skill.name} DOT", "dot", skill.dot_duration, skill.dot, 1.0, damage_type=skill.damage_type, source_id=source.id))
                if skill.hot > 0 and skill.hot_duration > 0:
                    effects.append(StatusEffect(f"{skill.id}:hot", f"{skill.name} HOT", "hot", skill.hot_duration, skill.hot, 1.0, source_id=source.id))
                for effect in effects:
                    effect = copy.deepcopy(effect)
                    effect.source_id = source.id
                    self._apply_effect(source, effect, now, log, rng)
            return
        if now < target.iframe_until:
            log.emit(now, CombatEventType.DODGE, source.id, target.id, skill_id=skill.id if skill else "",
                     metadata={"reason": "iframe"})
            source.combo = 0
            return
        dodge = min(0.95, max(0.0, self._stat(target, "dodge")))
        if rng.random() < dodge:
            log.emit(now, CombatEventType.DODGE, source.id, target.id, skill_id=skill.id if skill else "",
                     metadata={"reason": "dodge_chance", "chance": dodge})
            source.combo = 0
            return
        log.emit(now, CombatEventType.HIT, source.id, target.id, skill_id=skill.id if skill else "")
        if is_basic:
            base_damage, multiplier, damage_type = 0.0, 1.0, "physical"
            knockback, hitstun, hits = self._stat(source, "knockback"), self._stat(source, "hitstun"), 1
            effects: List[StatusEffect] = []
            dot = dot_duration = hot = hot_duration = 0.0
        else:
            assert skill is not None
            base_damage, multiplier, damage_type = skill.damage, skill.multiplier, skill.damage_type
            knockback, hitstun, hits = skill.knockback or self._stat(source, "knockback"), skill.hitstun or self._stat(source, "hitstun"), max(1, skill.hits)
            effects = list(skill.status_effects)
            dot, dot_duration, hot, hot_duration = skill.dot, skill.dot_duration, skill.hot, skill.hot_duration
            if dot > 0 and dot_duration > 0:
                effects.append(StatusEffect(f"{skill.id}:dot", f"{skill.name} DOT", "dot", dot_duration, dot, 1.0, damage_type=damage_type, source_id=source.id))
            if hot > 0 and hot_duration > 0:
                effects.append(StatusEffect(f"{skill.id}:hot", f"{skill.name} HOT", "hot", hot_duration, hot, 1.0, source_id=source.id))
        for hit_index in range(hits):
            if not target.alive:
                break
            critical = bool(skill is None or skill.crit_allowed) and rng.random() < min(1.0, max(0.0, self._stat(source, "critical")))
            damage_below_half_hp = self._conditional_damage_bonus(source, target)
            values = {
                "attack": self._stat(source, "attack"), "defense": self._stat(target, "defense"),
                "multiplier": multiplier, "base_damage": base_damage,
                "critical_multiplier": self._stat(source, "crit_multiplier"),
                "critical": self._stat(source, "critical"), "is_critical": 1 if critical else 0,
                "target_hp": target.hp, "target_max_hp": target.max_hp, "source_hp": source.hp,
                "source_max_hp": source.max_hp, "combo": source.combo, "stacks": 1,
                "damage_below_half_hp": damage_below_half_hp,
                "distance": abs(source.position - target.position), "resistance": target.stats.resistances.get(damage_type, 0.0),
                "hit_count": hit_index + 1, "time": now,
            }
            expression = _effective_formula(source, cfg)
            # The bundled baseline is hot in every synthetic run.  Evaluate it
            # directly while retaining the restricted AST engine for custom
            # formulas; this keeps 10,000-run sweeps practical without changing
            # the numerical rule.
            if expression.replace(" ", "") in {
                "attack*multiplier+base_damage-defense",
                "max(0,attack*multiplier+base_damage-defense)",
                "attack*multiplier-defense",
                "max(0,attack*multiplier-defense)",
            }:
                raw = max(0.0, values["attack"] * multiplier + base_damage - values["defense"])
            else:
                try:
                    raw = self.formulas.evaluate(expression, values)
                except FormulaError:
                    # Invalid user formulas should not crash a long sweep.  Fall
                    # back to a transparent baseline and expose the issue in replay.
                    raw = max(0.0, values["attack"] * multiplier + base_damage - values["defense"])
                    log.emit(now, CombatEventType.DAMAGE, source.id, target.id, skill_id=skill.id if skill else "",
                             metadata={"formula_error": expression})
            # Keep this multiplier outside the user formula so a passive works
            # with both the bundled baseline and custom safe expressions.
            raw *= max(0.0, 1.0 + damage_below_half_hp)
            if critical and not _formula_controls_critical(expression):
                raw *= max(0.0, self._stat(source, "crit_multiplier"))
                log.emit(now, CombatEventType.CRITICAL, source.id, target.id, amount=raw, skill_id=skill.id if skill else "")
            resistance = target.stats.resistances.get(damage_type, 0.0)
            amount = max(0.0, raw * max(0.0, 1.0 - resistance))
            absorbed = self._consume_shield(target, amount)
            hp_damage = amount - absorbed
            if absorbed > 0:
                log.emit(now, CombatEventType.SHIELD_ABSORBED, source.id, target.id, amount=absorbed, skill_id=skill.id if skill else "")
            target.hp = max(0.0, target.hp - hp_damage)
            source.total_damage += hp_damage
            target.damage_taken += hp_damage
            log.emit(now, CombatEventType.DAMAGE, source.id, target.id, amount=hp_damage, skill_id=skill.id if skill else "",
                     metadata={"raw": raw, "critical": critical, "absorbed": absorbed, "damage_type": damage_type})
            if hp_damage > 0:
                source.combo += max(1, skill.combo_step if skill is not None and skill.combo_step else 1)
                log.emit(now, CombatEventType.COMBO_STEP, source.id, target.id, amount=source.combo, skill_id=skill.id if skill else "")
            if target.hp <= 0:
                target.alive = False
                target.deaths += 1
                log.emit(now, CombatEventType.DEATH, source.id, target.id, skill_id=skill.id if skill else "")
                break
        if not target.alive:
            return
        knockback_resistance = min(1.0, max(0.0, self._stat(target, "knockback_resistance")))
        effective_knockback = max(0.0, knockback) * (1.0 - knockback_resistance)
        if effective_knockback:
            direction = 1 if target.position >= source.position else -1
            target.position += direction * effective_knockback
            target.position = max(-cfg.arena_width / 2, min(cfg.arena_width / 2, target.position))
            log.emit(now, CombatEventType.KNOCKBACK, source.id, target.id, amount=effective_knockback, skill_id=skill.id if skill else "")
        hitstun_resistance = min(1.0, max(0.0, self._stat(target, "hitstun_resistance")))
        effective_hitstun = max(0.0, hitstun) * (1.0 - hitstun_resistance)
        if effective_hitstun > 0:
            target.hitstun_until = max(target.hitstun_until, now + effective_hitstun)
            log.emit(now, CombatEventType.HITSTUN, source.id, target.id, amount=effective_hitstun, skill_id=skill.id if skill else "")
        if cfg.enable_status_effects:
            for effect in effects:
                effect = copy.deepcopy(effect)
                effect.source_id = source.id
                self._apply_effect(target, effect, now, log, rng)

    def _apply_effect(
        self,
        target: _RuntimeActor,
        effect: StatusEffect,
        now: float,
        log: EventLog,
        rng: random.Random | None = None,
    ) -> None:
        chance = min(1.0, max(0.0, float(getattr(effect, "apply_chance", 1.0))))
        if chance < 1.0 and (rng is not None and rng.random() >= chance):
            kind = effect.kind.lower()
            event_type = CombatEventType.DEBUFF_APPLIED if kind in {"debuff", "dot", "stun"} else CombatEventType.BUFF_APPLIED
            log.emit(
                now,
                event_type,
                effect.source_id,
                target.id,
                metadata={"effect_id": effect.id, "kind": effect.kind, "applied": False, "chance": chance},
            )
            return
        self._invalidate_stats(target)
        kind = effect.kind.lower()
        if kind == "shield":
            shield_received = max(-1.0, self._stat(target, "shield_received"))
            amount = max(0.0, effect.magnitude * (1.0 + shield_received))
            expires = now + max(0.0, effect.duration) if effect.duration > 0 else float("inf")
            target.shield_layers.append((expires, amount, effect.source_id, effect.id))
            target.shield += amount
            log.emit(now, CombatEventType.BUFF_APPLIED, effect.source_id, target.id, amount=amount,
                     metadata={"effect_id": effect.id, "kind": effect.kind, "shield": target.shield})
            return
        existing = next((x for x in target.effects if x.effect.id == effect.id), None)
        if existing:
            existing.remaining_stacks = min(effect.max_stacks, existing.remaining_stacks + max(1, effect.stacks))
            existing.expires_at = max(existing.expires_at, now + effect.duration)
            existing.next_tick = min(existing.next_tick, now + max(0.01, effect.tick_interval))
        else:
            target.effects.append(_ActiveEffect(copy.deepcopy(effect), now + max(0.0, effect.duration), now + max(0.01, effect.tick_interval), max(1, effect.stacks)))
        if kind == "stun":
            resistance = min(1.0, max(0.0, self._stat(target, "hitstun_resistance")))
            effective_duration = max(0.0, effect.duration) * (1.0 - resistance)
            if effective_duration > 0:
                target.hitstun_until = max(target.hitstun_until, now + effective_duration)
                log.emit(now, CombatEventType.HITSTUN, effect.source_id, target.id, amount=effective_duration,
                         metadata={"effect_id": effect.id})
        event_type = CombatEventType.DEBUFF_APPLIED if kind in {"debuff", "dot", "stun"} else CombatEventType.BUFF_APPLIED
        log.emit(now, event_type, effect.source_id, target.id, amount=effect.magnitude,
                 metadata={"effect_id": effect.id, "kind": effect.kind, "duration": effect.duration})

    def _expire_shields(self, actor: _RuntimeActor, now: float, log: EventLog) -> None:
        if not actor.shield_layers:
            return
        kept: List[Tuple[float, float, str, str]] = []
        for expires, amount, source_id, effect_id in actor.shield_layers:
            if now + 1e-9 >= expires:
                log.emit(now, CombatEventType.BUFF_EXPIRED, source_id, actor.id, amount=amount, metadata={"effect_id": effect_id, "kind": "shield"})
            else:
                kept.append((expires, amount, source_id, effect_id))
        actor.shield_layers = kept
        actor.shield = sum(layer[1] for layer in kept)

    @staticmethod
    def _consume_shield(actor: _RuntimeActor, amount: float) -> float:
        remaining = max(0.0, amount)
        absorbed = min(actor.shield, remaining)
        if absorbed <= 0:
            return 0.0
        if not actor.shield_layers:
            actor.shield -= absorbed
            return absorbed
        updated: List[Tuple[float, float, str, str]] = []
        to_consume = absorbed
        for index, (expires, layer_amount, source_id, effect_id) in enumerate(actor.shield_layers):
            if to_consume > 0:
                used = min(layer_amount, to_consume)
                layer_amount -= used
                to_consume -= used
            if layer_amount > 1e-9:
                updated.append((expires, layer_amount, source_id, effect_id))
            if to_consume <= 1e-9:
                # Preserve layers after the consumed one unchanged.
                updated.extend(actor.shield_layers[index + 1:])
                break
        actor.shield_layers = updated
        actor.shield = max(0.0, actor.shield - absorbed)
        return absorbed

    def _tick_effects(
        self,
        actor: _RuntimeActor,
        now: float,
        log: EventLog,
        cfg: SimulationConfig,
        rng: random.Random,
        actors: Mapping[str, _RuntimeActor] | None = None,
    ) -> None:
        kept: List[_ActiveEffect] = []
        for active in actor.effects:
            effect = active.effect
            kind = effect.kind.lower()
            # A fast aggregate step can jump over several periodic ticks.  Run
            # every due tick (with a defensive cap for pathological configs)
            # instead of collapsing DOT/HOT into an instantaneous lump.
            if kind in {"dot", "hot"} and active.next_tick <= now + 1e-9 and active.next_tick <= active.expires_at + 1e-9:
                interval = max(0.01, effect.tick_interval)
                due_until = min(now, active.expires_at)
                due_count = int(math.floor((due_until - active.next_tick) / interval + 1e-9)) + 1
                due_count = max(0, min(due_count, 10000))
                for _ in range(due_count):
                    raw_amount = max(0.0, effect.magnitude) * max(1, active.remaining_stacks)
                    if kind == "dot":
                        resistance = actor.stats.resistances.get(effect.damage_type, 0.0)
                        amount = max(0.0, raw_amount * max(0.0, 1.0 - resistance))
                        absorbed = self._consume_shield(actor, amount)
                        hp_damage = amount - absorbed
                        actor.hp = max(0.0, actor.hp - hp_damage)
                        actor.damage_taken += hp_damage
                        # Attribute periodic damage to the caster as well as the
                        # victim.  Direct hits already update ``source.total_damage``;
                        # omitting DOT ticks made regular replay fights report lower
                        # DPS than the accelerated aggregate path.
                        source = actors.get(effect.source_id) if actors is not None else None
                        if source is not None and source is not actor:
                            source.total_damage += hp_damage
                        if absorbed > 0:
                            log.emit(now, CombatEventType.SHIELD_ABSORBED, effect.source_id, actor.id, amount=absorbed,
                                     metadata={"effect_id": effect.id, "damage_type": effect.damage_type})
                        log.emit(now, CombatEventType.DOT_TICK, effect.source_id, actor.id, amount=hp_damage,
                                 metadata={"effect_id": effect.id, "raw": raw_amount, "absorbed": absorbed, "damage_type": effect.damage_type})
                        if actor.hp <= 0:
                            actor.alive = False
                            actor.deaths += 1
                            log.emit(now, CombatEventType.DEATH, effect.source_id, actor.id, metadata={"cause": effect.id})
                            active.next_tick += interval
                            break
                    else:
                        amount = raw_amount
                        healed = min(amount, max(0.0, actor.max_hp - actor.hp))
                        actor.hp += healed
                        log.emit(now, CombatEventType.HOT_TICK, effect.source_id, actor.id, amount=healed, metadata={"effect_id": effect.id})
                    active.next_tick += interval
                # If a maliciously large duration generated more due ticks than
                # the safety cap, skip the omitted historical ticks rather than
                # looping forever on the same timestamp.
                if due_count >= 10000 and active.next_tick <= now:
                    active.next_tick = now + interval
            if now + 1e-9 >= active.expires_at and effect.duration >= 0:
                event_type = CombatEventType.DEBUFF_EXPIRED if kind in {"debuff", "dot", "stun"} else CombatEventType.BUFF_EXPIRED
                log.emit(now, event_type, effect.source_id, actor.id, metadata={"effect_id": effect.id})
                continue
            kept.append(active)
        actor.effects = kept
        self._invalidate_stats(actor)

    def _apply_phase(
        self,
        actor: _RuntimeActor,
        phase: BossPhase,
        now: float,
        log: EventLog,
        rng: random.Random | None = None,
        enable_status_effects: bool = True,
    ) -> None:
        if phase.id in actor.phase_applied:
            return
        actor.phase_applied.append(phase.id)
        actor.phase_index = max(actor.phase_index, len(actor.phase_applied) - 1)
        phase_modifiers = dict(phase.modifiers)
        # Accept both the compact phase fields and the extensible modifier map.
        # An explicit map entry wins when both spellings are present.
        if "attack_multiplier" not in phase_modifiers and phase.attack_multiplier != 1.0:
            phase_modifiers["attack_multiplier"] = phase.attack_multiplier
        if "defense_multiplier" not in phase_modifiers and phase.defense_multiplier != 1.0:
            phase_modifiers["defense_multiplier"] = phase.defense_multiplier
        if phase.skills:
            catalog = actor.skill_catalog or actor.skills
            by_id = {str(skill.id): skill for skill in catalog}
            selected = [by_id[str(skill_id)] for skill_id in phase.skills if str(skill_id) in by_id]
            if selected:
                actor.skills = selected
        if phase.skill_interval is not None:
            actor.phase_skill_interval = max(0.0, phase.skill_interval)
            actor.next_phase_skill_at = max(actor.next_phase_skill_at, now)
        for key, value in phase_modifiers.items():
            # Phase modifiers are represented as permanent active buffs.  This
            # keeps them visible in the same stat path as regular effects.
            effect = StatusEffect(f"phase:{phase.id}", phase.name or phase.id, "buff", 0.0, 0.0, modifiers={key: value})
            actor.effects.append(_ActiveEffect(effect, float("inf"), float("inf"), 1))
        if enable_status_effects:
            for status in phase.status_effects:
                effect = copy.deepcopy(status)
                effect.source_id = actor.id
                self._apply_effect(actor, effect, now, log, rng)
        self._invalidate_stats(actor)
        log.emit(now, CombatEventType.PHASE_CHANGED, actor.id, actor.id, metadata={"phase_id": phase.id, "phase_index": actor.phase_index, "modifiers": phase_modifiers})

    @staticmethod
    def _phase_triggered(
        actor: _RuntimeActor,
        phase: BossPhase,
        now: float,
        seen_types: Iterable[str],
    ) -> bool:
        """Return whether a phase's declarative trigger is active.

        A phase with no trigger is an explicit baseline and starts at t=0.  A
        phase that names a time, HP, or event trigger waits for that condition;
        this keeps replay and accelerated runs aligned.
        """
        has_trigger = phase.hp_threshold is not None or phase.time_threshold is not None or bool(phase.event)
        triggered = not has_trigger
        if phase.hp_threshold is not None:
            threshold = phase.hp_threshold * actor.max_hp if phase.hp_threshold <= 1 else phase.hp_threshold
            triggered = triggered or actor.hp <= threshold
        if phase.time_threshold is not None:
            triggered = triggered or now >= phase.time_threshold
        if phase.event:
            marker = str(phase.event).lower()
            if marker in {"always", "on_time", "on_hp"}:
                triggered = triggered or (phase.time_threshold is None and phase.hp_threshold is None)
            else:
                triggered = triggered or marker in {str(value).lower() for value in seen_types}
        return triggered

    def _check_phase(
        self,
        actor: _RuntimeActor,
        phases: Sequence[BossPhase],
        now: float,
        log: EventLog,
        rng: random.Random | None = None,
        enable_status_effects: bool = True,
    ) -> None:
        # A phase can be selected by HP fraction/absolute HP, elapsed time, or a
        # simple event marker.  Phases are evaluated in listed order and never
        # regress.
        policy = str(getattr(actor.spec, "phase_policy", "sequential")).lower()
        sequential = policy not in {"independent", "all"}
        first_matching = policy in {"first_matching_threshold", "first_matching"}
        for index, phase in enumerate(phases):
            if phase.id in actor.phase_applied:
                continue
            if self._phase_triggered(actor, phase, now, log.seen_types):
                self._apply_phase(actor, phase, now, log, rng, enable_status_effects)
                if first_matching:
                    break
                continue
            if sequential:
                break


def run_fight(player: Build | Mapping[str, Any], enemy: Enemy | Build | Mapping[str, Any], config: SimulationConfig | Mapping[str, Any] | None = None, seed: Optional[int] = None) -> FightResult:
    return CombatSimulator(config).simulate_fight(player, enemy, seed=seed)


def run_batch(player: Build | Mapping[str, Any], enemy: Enemy | Build | Mapping[str, Any], runs: int = 100, config: SimulationConfig | Mapping[str, Any] | None = None, seed: Optional[int] = None, keep_fights: bool = True) -> BatchResult:
    return CombatSimulator(config).run_batch(player, enemy, runs=runs, seed=seed, keep_fights=keep_fights)


class SimulationEngine:
    """Small adapter used by the Tk workbench and headless CLI.

    The engine accepts the editor's compact dictionaries and delegates every
    actual hit, status tick, phase transition, and event to
    :class:`CombatSimulator`.  Keeping this adapter here means the UI cannot
    silently drift into a separate calculator implementation.
    """

    def __init__(self, config: SimulationConfig | Mapping[str, Any] | None = None) -> None:
        self.config = config if isinstance(config, SimulationConfig) else SimulationConfig.from_dict(config or {})
        self.simulator = CombatSimulator(self.config)
        self.last_events: List[Dict[str, Any]] = []
        self.last_batch: Optional[BatchResult] = None

    @staticmethod
    def _skill_catalog(config: Mapping[str, Any]) -> Dict[str, Skill]:
        raw = config.get("skills", []) or []
        if isinstance(raw, Mapping):
            raw = [dict(v, id=k) if isinstance(v, Mapping) else {"id": k} for k, v in raw.items()]
        result: Dict[str, Skill] = {}
        for item in raw:
            skill = item if isinstance(item, Skill) else Skill.from_dict(item)
            result[skill.id] = skill
        return result

    @classmethod
    def _player(cls, value: Any, config: Mapping[str, Any]) -> Build:
        if isinstance(value, Build):
            return copy.deepcopy(value)
        raw = dict(value or {})
        catalogue = cls._skill_catalog(config)
        skills = []
        for item in raw.get("skills", []) or []:
            if isinstance(item, str) and item in catalogue:
                skills.append(catalogue[item].to_dict())
            else:
                skills.append(item)
        raw["skills"] = skills
        return Build.from_dict(raw)

    @classmethod
    def _enemy(cls, value: Any, config: Mapping[str, Any]) -> Enemy:
        if isinstance(value, Enemy):
            return copy.deepcopy(value)
        raw = dict(value or {})
        catalogue = cls._skill_catalog({"skills": config.get("enemy_skills", config.get("skills", []))})
        skills = []
        for item in raw.get("skills", []) or []:
            if isinstance(item, str) and item in catalogue:
                skills.append(catalogue[item].to_dict())
            else:
                skills.append(item)
        raw["skills"] = skills
        return Enemy.from_dict(raw)

    @staticmethod
    def _ui_event(event: Mapping[str, Any]) -> Dict[str, Any]:
        metadata = dict(event.get("metadata", {}) or {})
        return {
            "time": event.get("time", 0.0),
            "type": event.get("type", event.get("event_type", "Event")),
            "actor": event.get("source_id", event.get("actor", "")),
            "target": event.get("target_id", event.get("target", "")),
            "value": event.get("amount", event.get("value", 0.0)),
            "skill": event.get("skill_id", event.get("skill", "")),
            "metadata": metadata,
        }

    def run_fights(self, config: Mapping[str, Any], runs: int = 100, capture: bool = False) -> Dict[str, Any]:
        player = self._player(config.get("build") or config.get("player"), config)
        enemy = self._enemy(config.get("enemy") or config.get("target"), config)
        run_count = max(1, int(runs))
        raw_cfg = dict(config.get("simulation", {}) or {})
        for key in ("max_time", "time_step", "formula", "starting_distance", "arena_width",
                    "allow_movement", "enable_phases", "enable_status_effects",
                    "basic_attack_range", "engagement_distance", "dodge_cost",
                    "dodge_recovery", "fast_enemy_exposure"):
            if key in config and key not in raw_cfg:
                raw_cfg[key] = config[key]
        raw_cfg["seed"] = config.get("seed", raw_cfg.get("seed", self.config.seed))
        # Keep aggregate memory bounded.  A capture request gets one explicit
        # representative replay below; it must not retain 10,000 full fights.
        raw_cfg["record_replay"] = bool((capture and run_count <= 200) or (raw_cfg.get("record_replay", self.config.record_replay) and run_count <= 200))
        batch_config = SimulationConfig.from_dict(raw_cfg)
        batch = self.simulator.run_batch(
            player, enemy, runs=run_count, config=batch_config,
            seed=raw_cfg.get("seed"), keep_fights=run_count <= 200,
        )
        self.last_batch = batch
        first = batch.fights[0] if batch.fights else None
        if capture:
            replay_config = SimulationConfig.from_dict({**raw_cfg, "record_replay": True})
            sample = self.simulator.simulate_fight(player, enemy, config=replay_config, seed=raw_cfg.get("seed"))
            self.last_events = [self._ui_event(e) for e in sample.events]
        else:
            self.last_events = [self._ui_event(e) for e in (first.events if first else [])]
        p95_ttk = batch.p95_ttk if batch.p95_ttk is not None else 0.0
        summary = {
            "runs": batch.runs, "wins": batch.wins, "win_rate": batch.win_rate,
            "avg_ttk": batch.average_ttk or 0.0, "median_ttk": batch.median_ttk or 0.0,
            "p95_ttk": p95_ttk,
            "avg_dps": batch.average_dps, "avg_damage_taken": batch.average_damage_taken,
            "skill_usage": batch.skill_usage, "deaths": batch.player_deaths,
        }
        return {
            "results": [
                {"fight_id": i + 1, "won": f.win, "ttk": f.duration, "dps": f.dps,
                 "damage_taken": f.damage_taken, "skills_used": sum(f.skill_usage.values()),
                 "deaths": f.player_deaths}
                for i, f in enumerate(batch.fights)
            ],
            "summary": summary,
            "events": self.last_events if capture else [],
            "replay": self.last_events if capture else [],
        }

    def run_sweep(self, config: Mapping[str, Any], runs: int = 100) -> Dict[str, Any]:
        builds = list(config.get("builds", []) or [])
        if not builds and config.get("build"):
            builds = [config["build"]]
        hps = list(config.get("boss_hp_values", config.get("hp_values", [5000, 7500, 10000, 12500])))
        base_enemy = dict(config.get("enemy") or {})
        cells: List[Dict[str, Any]] = []
        for bi, build in enumerate(builds):
            for hp in hps:
                enemy = copy.deepcopy(base_enemy)
                if isinstance(enemy.get("stats"), Mapping):
                    enemy["stats"] = dict(enemy["stats"]); enemy["stats"]["hp"] = hp; enemy["stats"]["max_hp"] = hp
                else:
                    enemy["hp"] = hp; enemy["max_hp"] = hp
                output = self.run_fights({
                    **config,
                    "build": build,
                    "enemy": enemy,
                    "seed": int(config.get("seed", 0)) + bi * 10000 + int(float(hp)),
                    "record_replay": False,
                    "simulation": dict(config.get("simulation", {}) or {}),
                }, runs, capture=False)
                summary = output["summary"]
                cells.append({"build": build.get("name", build.get("id", "Build")) if isinstance(build, Mapping) else str(build), "boss_hp": hp, **summary})
        return {"cells": cells, "boss_hp_values": hps, "build_names": [b.get("name", b.get("id", "Build")) if isinstance(b, Mapping) else str(b) for b in builds]}


__all__ = ["FightResult", "BatchResult", "CombatSimulator", "SimulationEngine", "run_fight", "run_batch"]
