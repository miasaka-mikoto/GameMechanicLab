"""Reproducible experiment records and lightweight report generation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Mapping


def _plain(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


@dataclass
class ExperimentRecord:
    """An immutable-on-disk snapshot of one balance experiment."""

    id: str
    name: str
    hypothesis: str
    config: dict[str, Any]
    runs: int
    results: dict[str, Any] = field(default_factory=dict)
    conclusion: str = ""
    research_question: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status: str = "completed"

    def to_dict(self) -> dict[str, Any]:
        return _plain(asdict(self))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ExperimentRecord":
        d = dict(data)
        return cls(
            id=str(d.get("id", d.get("name", "experiment"))),
            name=str(d.get("name", d.get("id", "Experiment"))),
            hypothesis=str(d.get("hypothesis", "")),
            config=_plain(d.get("config", {})),
            runs=max(1, int(d.get("runs", 1))),
            results=_plain(d.get("results", {})),
            conclusion=str(d.get("conclusion", "")),
            research_question=str(d.get("research_question", d.get("researchQuestion", ""))),
            created_at=str(d.get("created_at", d.get("createdAt", datetime.now(timezone.utc).isoformat()))),
            status=str(d.get("status", "completed")),
        )


def save_experiment(record: ExperimentRecord | Mapping[str, Any], path: str | Path) -> Path:
    """Save an experiment record as portable UTF-8 JSON."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = record.to_dict() if isinstance(record, ExperimentRecord) else _plain(record)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def load_experiment(path: str | Path) -> ExperimentRecord:
    return ExperimentRecord.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def render_report(record: ExperimentRecord | Mapping[str, Any]) -> str:
    data = record.to_dict() if isinstance(record, ExperimentRecord) else _plain(record)
    result = data.get("results", {}) if isinstance(data, Mapping) else {}
    lines = [f"# {data.get('name', 'Experiment')}", "", "## Research question", "", str(data.get("research_question", "")) or "(not recorded)", "", "## Hypothesis", "", str(data.get("hypothesis", "")) or "(not recorded)", "", "## Execution", "", f"- Runs: {data.get('runs', 0)}", f"- Status: {data.get('status', 'completed')}", "", "## Results", "", "```json", json.dumps(result, ensure_ascii=False, indent=2), "```", "", "## Conclusion", "", str(data.get("conclusion", "")) or "Pending interpretation.", ""]
    return "\n".join(lines)


__all__ = ["ExperimentRecord", "save_experiment", "load_experiment", "render_report"]

