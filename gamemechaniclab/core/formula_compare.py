"""Paired simulation helpers for comparing damage formula variants.

Formula Lab users usually want to compare two or more rules while holding the
rest of the experiment constant.  This module keeps that operation in the
simulation layer (rather than making the UI reimplement it): every expression
is validated by :class:`~gamemechaniclab.core.formula.FormulaEngine` first and
each variant is then run with the same per-fight seed sequence.  The result is
portable JSON data with aggregate metrics and deltas against a selected
baseline.

The helper deliberately accepts only formula *text* and ordinary model/config
objects.  It never executes arbitrary Python source and it never mutates the
caller-owned build, enemy, or simulation configuration.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from numbers import Real
from typing import Any

from .formula import FormulaEngine
from .models import Build, Enemy, SimulationConfig
from .simulation import run_batch


_DELTA_METRICS = (
    "win_rate",
    "average_ttk",
    "median_ttk",
    "average_dps",
    "average_damage_taken",
    "deaths",
)


def _variant_items(formulas: str | Mapping[str, Any] | Sequence[Any]) -> list[tuple[str, str]]:
    """Normalize the supported formula-variant spellings.

    The preferred form is ``{"baseline": "...", "candidate": "..."}``.
    A sequence may contain expression strings, ``(label, expression)`` pairs,
    or small mappings with ``id``/``name`` and ``formula``/``expression``
    fields.  Labels are intentionally explicit in the mapping form so they
    remain stable when a result is exported or plotted.
    """

    if isinstance(formulas, str):
        # A single expression is useful for programmatic callers and should
        # not be interpreted as a sequence of one-character variants.
        raw_items = [("formula_1", formulas)]
    elif isinstance(formulas, Mapping):
        raw_items = list(formulas.items())
    else:
        raw_items = []
        for index, item in enumerate(formulas):
            if isinstance(item, Mapping):
                label = item.get("id", item.get("name", item.get("label", f"formula_{index + 1}")))
                expression = item.get("formula", item.get("expression", item.get("value", "")))
                raw_items.append((label, expression))
            elif isinstance(item, (tuple, list)) and len(item) == 2:
                raw_items.append((item[0], item[1]))
            else:
                raw_items.append((f"formula_{index + 1}", item))

    if not raw_items:
        raise ValueError("at least one formula variant is required")
    items: list[tuple[str, str]] = []
    labels: set[str] = set()
    for label, expression in raw_items:
        normalized_label = str(label).strip()
        if not normalized_label:
            raise ValueError("formula variant labels cannot be empty")
        if normalized_label in labels:
            raise ValueError(f"duplicate formula variant label: {normalized_label}")
        if expression is None or not str(expression).strip():
            raise ValueError(f"formula variant '{normalized_label}' is empty")
        labels.add(normalized_label)
        items.append((normalized_label, str(expression)))
    return items


def _numeric_delta(value: Any, baseline: Any) -> float | None:
    if isinstance(value, Real) and not isinstance(value, bool) and isinstance(baseline, Real) and not isinstance(baseline, bool):
        return float(value) - float(baseline)
    return None


def compare_formula_variants(
    player: Build | Mapping[str, Any],
    enemy: Enemy | Mapping[str, Any],
    formulas: str | Mapping[str, Any] | Sequence[Any],
    *,
    runs: int = 100,
    seed: int | None = None,
    config: SimulationConfig | Mapping[str, Any] | None = None,
    baseline: str | None = None,
) -> dict[str, Any]:
    """Run paired batches for multiple safe damage formulas.

    ``seed`` (or ``config.seed``) is reused for every formula, so run ``i`` in
    each row receives the same random seed.  This paired design makes deltas
    substantially easier to interpret when criticals, dodges, or phase rolls
    are stochastic.  If no seed is supplied the simulator still runs normally,
    but the returned metadata marks the comparison as unpaired.

    The returned ``formula_rows`` contain the ordinary :class:`BatchResult`
    metrics plus ``formula_id``, normalized ``formula``, and
    ``delta_vs_baseline``.  ``baseline_metrics`` is included separately for
    report writers that do not want to locate a row by label.
    """

    items = _variant_items(formulas)
    engine = FormulaEngine()
    compiled: list[tuple[str, str, str]] = []
    # Validate *all* expressions before starting any simulation.  A typo in a
    # later variant must not leave a partial, misleading result on disk.
    for label, expression in items:
        formula = engine.compile(expression)
        compiled.append((label, expression, formula.expression))

    base_config = config if isinstance(config, SimulationConfig) else SimulationConfig.from_dict(config or {})
    base_config = copy.deepcopy(base_config)
    base_seed = seed if seed is not None else base_config.seed
    run_count = max(1, int(runs))
    baseline_id = str(baseline or compiled[0][0])
    if baseline_id not in {label for label, _, _ in compiled}:
        raise ValueError(f"unknown baseline formula variant: {baseline_id}")

    rows: list[dict[str, Any]] = []
    for index, (label, raw_expression, normalized_expression) in enumerate(compiled):
        variant_config = copy.deepcopy(base_config)
        variant_config.formula = normalized_expression
        variant_config.formula_override = True
        # Formula comparisons are aggregate studies.  Keeping a full event log
        # for every variant would make a 10,000-run comparison unnecessarily
        # large; callers can request a separate replay with run_fight.
        variant_config.record_replay = False
        batch = run_batch(
            player,
            enemy,
            runs=run_count,
            config=variant_config,
            seed=base_seed,
            keep_fights=False,
        )
        payload = batch.to_dict(include_fights=False)
        payload.update(
            {
                "index": index,
                "formula_id": label,
                "formula_input": raw_expression,
                "formula": normalized_expression,
                "seed": base_seed,
                "paired_seed": base_seed is not None,
            }
        )
        rows.append(payload)

    baseline_row = next(row for row in rows if row["formula_id"] == baseline_id)
    for row in rows:
        row["delta_vs_baseline"] = {
            metric: _numeric_delta(row.get(metric), baseline_row.get(metric))
            for metric in _DELTA_METRICS
        }

    baseline_metrics = {metric: baseline_row.get(metric) for metric in _DELTA_METRICS}
    return {
        "baseline": baseline_id,
        "runs": run_count,
        "seed": base_seed,
        "paired_seed": base_seed is not None,
        "formula_rows": rows,
        "baseline_metrics": baseline_metrics,
        "metrics": list(_DELTA_METRICS),
    }


__all__ = ["compare_formula_variants"]
