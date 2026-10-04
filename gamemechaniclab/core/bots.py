"""Simple deterministic bot policies used by the combat simulator."""

from __future__ import annotations

from dataclasses import dataclass
import random
from typing import Any, Iterable, List, Mapping, Optional, Sequence

from .models import Skill


@dataclass
class BotDecision:
    """A policy decision made for one simulation tick."""

    action: str = "basic_attack"  # basic_attack, skill, dodge, wait
    skill: Optional[Skill] = None


class Bot:
    name = "base"

    def choose(self, actor: Any, target: Any, skills: Sequence[Skill], available: Sequence[Skill], rng: random.Random) -> BotDecision:
        return BotDecision()

    # Alias used by external integrations.
    def decide(self, actor: Any, target: Any, skills: Sequence[Skill], available: Sequence[Skill], rng: random.Random) -> BotDecision:
        return self.choose(actor, target, skills, available, rng)


def _skill_score(skill: Skill, actor: Any = None, target: Any = None) -> float:
    attack = float(getattr(getattr(actor, "stats", actor), "attack", 0.0))
    return max(0.0, skill.damage + attack * skill.multiplier) * max(1, skill.hits) / max(0.05, skill.cooldown + skill.cast_time + skill.recovery)


class AggressiveBot(Bot):
    name = "aggressive"

    def choose(self, actor: Any, target: Any, skills: Sequence[Skill], available: Sequence[Skill], rng: random.Random) -> BotDecision:
        if available:
            return BotDecision("skill", max(available, key=lambda s: _skill_score(s, actor, target)))
        return BotDecision("basic_attack")


class DefensiveBot(Bot):
    name = "defensive"

    def choose(self, actor: Any, target: Any, skills: Sequence[Skill], available: Sequence[Skill], rng: random.Random) -> BotDecision:
        hp = float(getattr(actor, "hp", getattr(getattr(actor, "stats", actor), "hp", 1.0)))
        max_hp = float(getattr(actor, "max_hp", getattr(getattr(actor, "stats", actor), "max_hp", 1.0)) or 1.0)
        # Prefer healing/shield/defensive effects when below 55%, otherwise use
        # the safest high-value skill.
        if available and hp / max_hp < 0.55:
            defensive = [s for s in available if s.hot > 0 or any(e.kind in {"hot", "shield", "buff"} for e in s.status_effects)]
            if defensive:
                return BotDecision("skill", max(defensive, key=lambda s: _skill_score(s, actor, target)))
        if available:
            return BotDecision("skill", max(available, key=lambda s: _skill_score(s, actor, target)))
        if getattr(actor, "can_dodge", False) and rng.random() < 0.25:
            return BotDecision("dodge")
        return BotDecision("basic_attack")


class RandomBot(Bot):
    name = "random"

    def choose(self, actor: Any, target: Any, skills: Sequence[Skill], available: Sequence[Skill], rng: random.Random) -> BotDecision:
        choices: List[BotDecision] = [BotDecision("basic_attack")]
        choices.extend(BotDecision("skill", s) for s in available)
        if getattr(actor, "can_dodge", False):
            choices.append(BotDecision("dodge"))
        return rng.choice(choices)


class OptimalRotationBot(Bot):
    name = "optimal"

    def choose(self, actor: Any, target: Any, skills: Sequence[Skill], available: Sequence[Skill], rng: random.Random) -> BotDecision:
        if not available:
            return BotDecision("basic_attack")
        # Deterministic greedy approximation: maximize expected damage per
        # second, then prefer the lower cost skill.
        return BotDecision("skill", max(available, key=lambda s: (_skill_score(s, actor, target), -s.cost)))


_BOT_TYPES = {
    "aggressive": AggressiveBot,
    "defensive": DefensiveBot,
    "random": RandomBot,
    "optimal": OptimalRotationBot,
    "optimalrotation": OptimalRotationBot,
    "optimal_rotation": OptimalRotationBot,
    "optimal rotation": OptimalRotationBot,
}


def get_bot(style: str | Bot | None) -> Bot:
    if isinstance(style, Bot):
        return style
    key = str(style or "aggressive").strip().lower()
    return _BOT_TYPES.get(key, AggressiveBot)()


__all__ = [
    "BotDecision", "Bot", "AggressiveBot", "DefensiveBot", "RandomBot",
    "OptimalRotationBot", "get_bot",
]
