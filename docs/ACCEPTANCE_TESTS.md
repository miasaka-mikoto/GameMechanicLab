# Game Mechanic Lab — acceptance matrix

Run `pytest -q` from the repository root. The checks below are the release gate; they deliberately exercise the simulation engine, not just UI widgets.

| ID | Area | Acceptance criterion | Evidence |
|---|---|---|---|
| A01 | Project loading | A YAML/JSON project loads without hard-coded balance values and round-trips to JSON | `test_data_driven_configs_and_round_trip`, `test_public_data_loader_reads_a_project_catalogue` |
| A02 | Real combat | One fight emits ordered combat events and reaches a terminal win/loss state | `test_batch_statistics_and_real_event_stream`, `test_arena_adapter_runs_core_fight_and_returns_replay` |
| A03 | Components | HP, attack/defense, crit, cooldown, dodge/i-frame, knockback, hitstun, DOT/HOT, shield, buff/debuff and combo affect a run | model schema + simulation event assertions, `test_dot_resistance_and_shield_absorb_periodic_damage`, `test_conditional_execution_passive_applies_in_regular_and_fast_paths` |
| A04 | Formula safety | Valid formulas evaluate; attribute access, imports, calls outside the allow-list, statements and resource-heavy expressions are rejected | `test_formula_lab_accepts_arithmetic_and_rejects_code` |
| A05 | Batch statistics | 1, 100 and 10,000 run modes are accepted and expose win rate, DPS, TTK, damage taken, usage, and deaths | `test_cli_accepts_all_required_run_sizes`, `test_batch_statistics_and_real_event_stream` |
| A06 | Seed reproducibility | Same config and seed produce byte-equivalent aggregate output and event trace | `test_seed_reproducibility_and_boss_phase_events` |
| A07 | Bots | Aggressive, defensive, random and optimal-rotation policies are selectable and produce valid decisions | `test_all_bot_policies_make_valid_decisions` |
| A08 | Sweep | Cartesian parameter combinations are all evaluated; each row retains parameter values and metrics | `test_parameter_sweep_runs_every_cartesian_cell_without_mutating_inputs` |
| A09 | Heatmap | Sweep can be projected to player-damage × boss-HP win-rate/TTK matrix | `test_heatmap_projection_keeps_axes_and_metric_matrix` |
| A10 | Replay | A run can be serialized and loaded with time, attack, damage, dodge, skill, buff/debuff and death events intact | `test_event_stream_order_and_replay_round_trip` |
| A11 | Warnings | Synthetic pathological configurations trigger infinite-combo, permanent-stun, 100%-crit, negative-damage, unkillable-build, DPS-explosion and cooldown-loop diagnostics | `test_balance_warnings_cover_pathological_mechanics` |
| A12 | Experiment | Hypothesis, config snapshot, runs, results, and conclusion are saved and reloadable | `test_experiment_record_save_load_and_report` |
| A13 | Build/enemy editor | Builds contain stats/equipment/skills/passives; enemies contain stats/AI/resistance/skills/phases | schema tests |
| A14 | Boss phases | HP/time/event transitions occur once, are logged, and alter behavior | `test_seed_reproducibility_and_boss_phase_events`, `test_phase_trigger_schema_is_sequential_and_multiplier_aware`, `test_event_triggered_boss_phase_works_without_replay_allocation` |
| A15 | UI smoke | Arena, simulator, charts, replay and warning views open with the bundled demo | `docs/capture_screenshots.py` and PNG artifact |

## Manual smoke run

```bash
python -m gamemechaniclab --demo --headless --runs 1
python -m gamemechaniclab --demo --headless --runs 100 --output artifacts/demo
python docs/capture_screenshots.py --output artifacts/screenshots
```

For a release candidate, repeat A05 at 10,000 runs in headless mode. Keep only an explicitly selected sample replay while aggregating the other fights so memory remains bounded; a full replay for every run is intentionally an opt-in diagnostic. Check the returned `simulation_mode`: high-volume, replay-disabled runs are the documented accelerated approximation, while a replay-enabled sample is the exact event-clock trace. Do not treat a static screenshot of a calculator as passing A02 or A15: the event stream, replay, and aggregate counters must come from the same combat simulation.

## Failure triage

When a check fails, retain the smallest failing YAML/JSON, seed, engine version, and replay JSON. A balance warning is not itself a test failure; the analyzer must detect it deterministically and explain the observed evidence.
