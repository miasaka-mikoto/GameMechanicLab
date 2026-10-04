# Game Mechanic Lab

**游戏机制与数值实验室** is a local, data-driven 2D combat sandbox for answering balance questions with repeatable simulations rather than intuition. It is a research tool, not a complete game: every run is deterministic when a seed is supplied, every event is recorded, and every experiment can be exported and reproduced.

The shipped build is fully local: the demo, bots, formula evaluator, simulator,
charts, and reports do not call a paid model API or require an internet
connection.

## What it can answer

- How much does a 0.3 s versus 0.5 s invulnerability window change survival?
- What boss HP gives a desired time-to-kill (TTK) range?
- Does a critical-hit, cooldown, or knockback rule create an outlier DPS build?
- Which build wins most often against a phase-based boss?
- Where do permanent stun, infinite combo, negative damage, or cooldown-loop problems appear?

## Quick start

The application uses only the Python standard library at runtime. The development/test dependency is `pytest`.

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
python -m pip install -e .
python app.py --demo
```

The demo opens the Combat Arena and loads the bundled experiment (3 builds, 5 skills, 3 enemies, and one three-phase boss). If a desktop window is not available, the same experiment can be run headlessly:

```bash
python app.py --demo --headless --runs 100       # compact fallback smoke run
python -m gamemechaniclab --demo --headless --runs 100  # full catalog + sweep
# A bare headless command benchmarks one real build-versus-boss matchup.
python -m gamemechaniclab --headless --runs 10000
pytest -q
```

The exact options are also shown by `python -m gamemechaniclab --help`.

The bundled `artifacts/final_demo/` directory is a generated reference run
(100 fights per scenario) containing JSON metrics, CSV sweep rows, win-rate,
TTK, and damage-vs-HP PNG/SVG heatmaps, a replay stream, and a Markdown
experiment report.  It can be
regenerated with:

```bash
python -m gamemechaniclab --demo --headless --runs 100 --output artifacts/final_demo
```

## Project layout

```text
gamemechaniclab/       simulation engine and UI
demo/                  data-driven demo experiment and sweep (YAML/JSON)
tests/                 unit and end-to-end acceptance tests
docs/                  acceptance matrix, user guide, report template, release manifest
artifacts/             generated charts, replays, and reports (ignored by git)
```

## Data-driven configuration

Mechanics are declared in YAML or JSON; the engine does not embed balance numbers in Python code. A minimal configuration looks like:

```yaml
project:
  id: invulnerability-study
  max_experiments: 10000
  seed: 42
builds:
  - id: sword-basic
    stats: {hp: 1000, attack: 100, defense: 20, attack_speed: 1.0}
    skills: [slash]
skills:
  - id: slash
    damage: 1.0
    cooldown: 1.0
    cost: 0
enemies:
  - id: dummy
    hp: 5000
    defense: 30
    ai_style: defensive
```

Supported components include HP, attack/defense, critical and attack speed, movement and cooldown, mana/stamina, i-frames and dodge, knockback/hitstun, projectile/AOE, DOT/HOT, buffs/debuffs, shields, and combos. Build, skill, enemy, resistance, and boss-phase definitions are all serializable.

## Formula Lab safety

Damage and other formulas are parsed by the restricted expression evaluator in `gamemechaniclab.core.formula`. Only numeric literals, approved variables, arithmetic operators, comparisons, and a small allow-list of math functions are accepted. User text is **never** passed to unrestricted `eval`, `exec`, a shell, or an import mechanism. Invalid names, attributes, calls, comprehensions, statements, and excessively deep expressions are rejected with a validation error.

Example:

```python
from gamemechaniclab.core.formula import FormulaEngine

formula = FormulaEngine().compile("max(0, attack * multiplier - defense)")
damage = formula.evaluate({"attack": 120, "multiplier": 1.5, "defense": 30})
```

The same safe Formula Lab is available from the headless runner.  Pass one
formula to a batch, or compare named variants with paired random seeds (so
critical/dodge rolls line up across variants):

```bash
python -m gamemechaniclab --headless --runs 100 \
  --formula "max(0, attack * multiplier - defense)" \
  --formula-variant "baseline=attack * multiplier" \
  --formula-variant "armored=max(0, attack * multiplier - defense * 1.2)" \
  --output artifacts/formula_study
```

This writes `formula_comparison.json` and `formula_comparison.csv`, including
win rate, DPS, mean/median/p95 TTK, damage taken, deaths, the normalized formula
text, and deltas against the first (or explicitly selected) variant.  Every variant is
validated before any fight starts. By default a formula is the base damage expression
and the simulator applies the rolled critical multiplier afterward; a formula that
references `is_critical` or `critical_multiplier` owns critical scaling and is not
multiplied again.

## Simulation and analysis

The engine exposes one-fight and batch modes, with the same event stream in both:

```python
from gamemechaniclab.core import Build, CombatSimulator, Enemy, SimulationConfig, Skill, run_batch

config = SimulationConfig(seed=7, max_time=180)
result = CombatSimulator(config).simulate_fight(build, enemy)
batch = run_batch(build, enemy, runs=100, config=config)
```

Batch output includes win rate, DPS, TTK (mean/median/p95 over successful
kills), damage taken, skill usage, failures (`deaths` for compatibility) and
actual `player_deaths`, plus event counts. If a batch has no successful kill,
its TTK fields are `null`. A sweep accepts lists/ranges for any numeric
parameter and produces one row per combination. The chart panel can render a
TTK or win-rate heatmap (for example, player damage × boss HP). A single run
can be opened in Combat Replay to inspect time, attack, damage, dodge, skill,
buff/debuff, and death events.

Every batch result labels its execution mode. `event_replay` is the full
event-clock path used for one-fight captures and small replay batches. A large
aggregate run with replay disabled is labelled `accelerated_approximation`: it
still resolves cooldowns, resources, crit/dodge rolls, phases, shields, and
periodic effects, but omits a continuous position trace and resolves projectile
travel at the aggregate impact step. The enemy exposure factor is an explicit
`mechanics.simulation_defaults.fast_enemy_exposure` value (the bundled study
uses `1.0`, matching full enemy outgoing damage). A project may lower it as a
documented calibration choice, so charts do not silently imply pixel-perfect
replay equivalence.

## Bots and boss phases

The built-in policies are `aggressive`, `defensive`, `random`, and a simplified `optimal_rotation`. An enemy may define `phase` transitions by HP threshold, elapsed time, or an emitted event, select a phase skill subset, and set a phase skill interval. Phase changes are logged as ordinary combat events, so replay and aggregate reports remain consistent.

## Balance warnings

The analyzer reports evidence, not a verdict. It checks for infinite combos, permanent stun, 100% critical chance, negative damage, unkillable builds, DPS explosions, and cooldown loops. Each warning includes the rule, observed counters, and the run/seed that reproduced it.

## Reproducibility and artifacts

Always record the configuration, seed, engine version, run count, and formula text with a result. Use the **Export experiment** action (or the `save_experiment` API) to write a JSON snapshot; sweep rows can be projected to CSV/PNG by a calling script. The report template in [`docs/EXPERIMENT_REPORT_TEMPLATE.md`](docs/EXPERIMENT_REPORT_TEMPLATE.md) is intentionally plain text so it can be committed with a study.

## Packaging

On Windows, run PowerShell from the project root to produce the requested
standalone executable:

```powershell
.\build_windows.ps1 -Clean
# output: dist\GameMechanicLab.exe
```

On Linux, build a native single-file executable and validate it with a real
headless fight:

```bash
./build_linux.sh --clean
./dist/GameMechanicLab-linux --headless --runs 1 --output /tmp/game-mechanic-lab-linux-smoke
```

The native Linux build includes Matplotlib/PyYAML by default for rich chart
exports.  Use `./build_linux.sh --skip-optional-packages` for a smaller
standard-library-only binary; JSON configs, SVG heatmaps, the Arena, replay,
simulator, and all balance analysis still work.

On macOS or on Linux machines where a single-file build is not desired, use
the dependency-light portable folder instead:

```bash
./build_portable.sh
dist/GameMechanicLab-portable/run.sh --headless --runs 100
```

The portable launcher keeps the source modules, JSON/YAML demo assets, and
reference charts together; the script also creates
`dist/GameMechanicLab-portable-linux.zip` when `zip` is available. Matplotlib
is optional; the core arena and Tk Canvas charts remain available without it.
PyYAML enables richer YAML files; the paired JSON files remain the
zero-dependency fallback. `load_project()` is the shared data boundary: skill
IDs and mechanics-registry status IDs (including phase `add_status`) are
expanded before model construction, while equipment/passive flat and
multiplier modifiers remain declarative.

## Development checks

```bash
pytest -q
python -m compileall gamemechaniclab
```

The acceptance matrix in [`docs/ACCEPTANCE_TESTS.md`](docs/ACCEPTANCE_TESTS.md) describes the required checks for a release. The concise handoff checklist and artifact inventory are in [`docs/RELEASE_MANIFEST.md`](docs/RELEASE_MANIFEST.md). A screenshot helper is available at `docs/capture_screenshots.py`; it captures the Arena, simulation summary, sweep chart, replay, and Formula Lab when the desktop UI is running. Warning diagnostics are available through the public analyzer API and are covered by the automated tests.

中文快速说明见 [`docs/USER_GUIDE_CN.md`](docs/USER_GUIDE_CN.md)。

## Scope and limitations

This is a deterministic approximation tool, not a claim about real player behavior or a network-authoritative game server. The shipped Arena is intentionally a one-player/one-target laboratory: AOE radius, knockback, and projectile metadata are simulated and replayed, while multi-target fan-out is left for a future encounter model. Collision and movement are lightweight, and high-volume batches carry an explicit approximation label. Use the results to compare rules and identify suspicious regions, then validate important decisions in the target game.
