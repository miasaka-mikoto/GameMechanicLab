"""Combat event model and replay-friendly event log."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional
import json


class CombatEventType(str, Enum):
    ATTACK_STARTED = "AttackStarted"
    HIT = "Hit"
    DAMAGE = "Damage"
    CRITICAL = "Critical"
    DODGE = "Dodge"
    BUFF_APPLIED = "BuffApplied"
    BUFF_EXPIRED = "BuffExpired"
    DEBUFF_APPLIED = "DebuffApplied"
    DEBUFF_EXPIRED = "DebuffExpired"
    SKILL_CAST = "SkillCast"
    SKILL_READY = "SkillReady"
    SHIELD_ABSORBED = "ShieldAbsorbed"
    HEAL = "Heal"
    DOT_TICK = "DotTick"
    HOT_TICK = "HotTick"
    KNOCKBACK = "Knockback"
    HITSTUN = "Hitstun"
    PHASE_CHANGED = "PhaseChanged"
    COMBO_STEP = "ComboStep"
    DEATH = "Death"
    FIGHT_STARTED = "FightStarted"
    FIGHT_ENDED = "FightEnded"


@dataclass(slots=True)
class CombatEvent:
    """A single immutable-ish record in a combat replay.

    The ``metadata`` field is intentionally open-ended, allowing future UI
    versions to display mechanic-specific details without changing the schema.
    """

    time: float
    type: CombatEventType | str
    source_id: str = ""
    target_id: str = ""
    amount: float = 0.0
    skill_id: str = ""
    message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.type, CombatEventType):
            return
        try:
            self.type = CombatEventType(str(self.type))
        except ValueError:
            self.type = str(self.type)
        self.time = float(self.time)
        self.amount = float(self.amount or 0.0)

    @property
    def event_type(self) -> str:
        return self.type.value if isinstance(self.type, CombatEventType) else str(self.type)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["type"] = self.event_type
        return d

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CombatEvent":
        d = dict(data)
        if "event_type" in d and "type" not in d:
            d["type"] = d.pop("event_type")
        return cls(**{k: d[k] for k in ("time", "type", "source_id", "target_id", "amount", "skill_id", "message", "metadata") if k in d})


class EventLog:
    """Append-only event stream with convenient replay/statistics queries."""

    def __init__(self, events: Optional[Iterable[CombatEvent]] = None, enabled: bool = True) -> None:
        self.enabled = bool(enabled)
        # Keep a tiny canonical marker index even when replay allocation is
        # disabled.  The accelerated simulation intentionally uses
        # ``EventLog(enabled=False)``; boss phases can still be configured to
        # react to an event such as ``SkillCast`` in that path.
        initial = list(events or [])
        self.events: List[CombatEvent] = initial if self.enabled else []
        self.seen_types: set[str] = {event.event_type.lower() for event in initial}

    def emit(
        self,
        time: float,
        event_type: CombatEventType | str,
        source_id: str = "",
        target_id: str = "",
        amount: float = 0.0,
        skill_id: str = "",
        message: str = "",
        **metadata: Any,
    ) -> CombatEvent | None:
        marker = event_type.value if isinstance(event_type, CombatEventType) else str(event_type)
        self.seen_types.add(marker.lower())
        if not self.enabled:
            return None
        # Callers often pass a ready-made ``metadata={...}`` mapping while
        # this convenience API also accepts keyword metadata.  Flatten that
        # common form so replay consumers see ``{"raw": ...}`` rather than
        # an accidental ``{"metadata": {"raw": ...}}`` wrapper.
        if len(metadata) == 1 and isinstance(metadata.get("metadata"), Mapping):
            metadata = dict(metadata["metadata"])
        event = CombatEvent(time, event_type, source_id, target_id, amount, skill_id, message, metadata)
        self.events.append(event)
        return event

    def append(self, event: CombatEvent) -> None:
        self.seen_types.add(event.event_type.lower())
        if self.enabled:
            self.events.append(event)

    def extend(self, events: Iterable[CombatEvent]) -> None:
        values = list(events)
        self.seen_types.update(event.event_type.lower() for event in values)
        if self.enabled:
            self.events.extend(values)

    def __iter__(self) -> Iterator[CombatEvent]:
        return iter(self.events)

    def __len__(self) -> int:
        return len(self.events)

    def __getitem__(self, item: int) -> CombatEvent:
        return self.events[item]

    def filter(self, event_type: CombatEventType | str) -> List[CombatEvent]:
        wanted = event_type.value if isinstance(event_type, CombatEventType) else str(event_type)
        return [e for e in self.events if e.event_type == wanted]

    def between(self, start: float = 0.0, end: Optional[float] = None) -> List[CombatEvent]:
        return [e for e in self.events if e.time >= start and (end is None or e.time <= end)]

    def count(self, event_type: CombatEventType | str) -> int:
        return len(self.filter(event_type))

    def first(self, event_type: CombatEventType | str) -> Optional[CombatEvent]:
        values = self.filter(event_type)
        return values[0] if values else None

    def to_list(self) -> List[Dict[str, Any]]:
        return [e.to_dict() for e in self.events]

    def to_json(self, **kwargs: Any) -> str:
        return json.dumps(self.to_list(), ensure_ascii=False, **kwargs)

    @classmethod
    def from_list(cls, records: Iterable[Mapping[str, Any]]) -> "EventLog":
        return cls(CombatEvent.from_dict(r) for r in records)


__all__ = ["CombatEventType", "CombatEvent", "EventLog"]
