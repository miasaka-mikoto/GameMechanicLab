# Clockwork Tyrant demo experiment

This is a complete, reproducible Game Mechanic Lab experiment rather than a
UI-only example. It compares three builds against three ordinary enemies and a
three-phase boss, records combat events, and produces a Boss HP/build sweep
plus player-damage/Boss HP heatmaps.

## Run

The JSON files are dependency-free and can be loaded with Python's standard
library. From the repository root, use the project's simulator command (or the
equivalent API):

```bash
python -m gamemechaniclab --demo --headless --runs 100
# Add --output to persist generated files.
python -m gamemechaniclab --demo --headless --runs 100 --output artifacts/demo
```

The desktop equivalent is `python app.py --demo`; use the **SWEEP** button after
the window opens. The compact `demo/*.json` and `demo/*.yaml` files are the
portable experiment specification: they document the hypotheses, dimensions,
acceptance thresholds, and intended run profiles. The CLI resolves the bundled
runtime catalogue from `configs/*.json` (so it works without PyYAML) and writes
the generated artifacts listed below. The paired JSON/YAML specifications are
validated for parity by the test suite and are safe to edit when extending the
experiment definition.

The manifest's `default_runs` is the planned study profile (1,000 replicates);
`cli_default_runs` records the lighter 100-run CLI default used for quick
interactive work and the bundled reference artifacts.

The simulator writes `demo_results.json`, `balance_sweep.csv`, a sample
`replay_sample.json`, win-rate/TTK/damage heatmaps (JSON/SVG and PNG when
Matplotlib is available), `demo_experiment_record.json`, and `demo_report.md`
in the requested output directory. A run with `--runs 100` is the bundled
reference profile; `--runs 10000` is supported for stress testing, with only
one representative replay retained.

## What the demo tests

- `H1`: high burst/critical scaling increases DPS but makes the Glass Cannon
  fragile at 10,000–12,500 boss HP.
- `H2`: changing only I-frame duration from 0.30 s to 0.50 s improves survival
  against telegraphed area attacks.
- `H3`: a healthy boss setting has 45–85% win rate and 25–55 s median TTK.

The declarative sweep specification defines 72 raw combinations, with a
36-cell execution cap after its filters. The filter keeps the default/
over-tuned damage multipliers and the I-frame comparison tractable while
preserving the full parameter definitions for the editor. The CLI's generated
reference sweep is intentionally smaller and directly comparable: three builds
× four Boss HP values (12 rows), plus a 4 × 4 player-attack/Boss-HP projection.
Use the `replicates_per_cell` value in `demo/sweep.json` when planning a larger
study; pass the actual run count explicitly with `--runs`.

The separate `iframe_comparison` scenario uses paired seeds so the 0.30 s and
0.50 s variants can be compared without a different random sequence masking
the mechanic change.

## Expected event stream

Each replay uses the common event names `AttackStarted`, `Hit`, `Damage`,
`Critical`, `Dodge`, `BuffApplied`, `BuffExpired`, `SkillCast`, `Death`, and
`PhaseChanged` (when a phase transition occurs). A replay row includes at
least `time`, `source_id`, `target_id`, `skill_id`, `amount`, and a `metadata`
object. This makes the same data usable by the arena timeline, aggregate
charts, and balance warnings.

## Output contract

The output names are intentionally stable so scripts and reports can consume a
run without depending on the desktop UI:

| File | Contents |
|---|---|
| `demo_results.json` | All 12 scenario aggregates, warnings, and sweep projections |
| `balance_sweep.csv` | Boss HP × build rows and core metrics |
| `win_rate_heatmap.*` | Boss HP × build win-rate matrix |
| `ttk_heatmap.*` | Boss HP × build median-TTK matrix |
| `damage_vs_boss_hp_heatmap.*` | Player attack × Boss HP median-TTK matrix |
| `damage_vs_boss_hp_win_rate_heatmap.*` | Player attack × Boss HP win-rate matrix |
| `replay_sample.json` | One event-complete representative combat replay |
| `demo_experiment_record.json` | Hypothesis/config/results/conclusion snapshot |
| `demo_report.md` | Human-readable generated experiment report |
