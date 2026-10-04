# Game Mechanic Lab data files

The files in this directory are the editable, data-driven layer of the lab. The
`.json` files are canonical runtime fallbacks (Python's standard library can
load them without PyYAML); the matching `.yaml` files are the same content in
human-friendly form for editing.

## Layout

| File | Contents |
|---|---|
| `mechanics` | Component registry, safe formulas, and status effects |
| `builds` | `balanced_vanguard`, `glass_cannon`, and `fortress_controller` |
| `skills` | Five player skills used by the demo |
| `enemies` | Training Dummy, Iron Skirmisher, and Ember Caster |
| `bosses` | Three-phase Clockwork Tyrant |

Every entity has a stable `id`. References use IDs rather than duplicated
objects: a build's `skills` list points into `skills`, and an enemy's `skills`
list points into `enemy_skills`. Numeric values are intentionally outside the
simulator code so a sweep can override a path such as
`bosses.clockwork_tyrant.stats.hp`.

`mechanics.simulation_defaults` is the editable home for arena action defaults
(`basic_attack_range`, `engagement_distance`, dodge cost/recovery, and the
aggregate `fast_enemy_exposure` calibration, `1.0` in the bundled study). A
value of `1.0` means the accelerated aggregate path applies the same enemy
outgoing damage exposure as the full event path; lowering it is an explicit
calibration choice for a deliberately forgiving approximation. `load_project()` expands skill
and status IDs at the data boundary, including phase `add_status` records, so
an inline catalogue edit does not silently become a zero-magnitude effect.
Equipment and passive records may use flat modifiers (`attack: 20`) or
multipliers (`attack_multiplier: 1.2`).

Stats keep both editor labels such as `critical_chance`/`dodge_chance` and
short runtime aliases `critical`/`dodge`. Skills use the runtime fields
`aoe` and numeric `cost` plus `cost_type`; `aoe_radius` is retained as an
explicit design label. This lets the same files work in the desktop editor and
through the plain dataclass API.

## Formula safety

`mechanics.formulas` contains expressions for the formula lab. They are data,
not Python. A runtime should parse them with the project's safe arithmetic
expression evaluator and expose only approved names/functions (`max`, `min`,
`abs`, and numeric variables). Never pass these strings to `eval` or execute
arbitrary code.

## Adding a build or skill

1. Add a record with a unique ID to the relevant file.
2. Reference that ID from a scenario or build.
3. Keep costs, cooldowns, cast/recovery times, and status effects explicit.
4. Run the demo's validation/tests before committing the config.

Large batches with replay disabled are reported as
`accelerated_approximation`; use a replay-enabled one-fight run when the exact
position/projectile event trace is the object of study.
