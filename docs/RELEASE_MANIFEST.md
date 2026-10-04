# Game Mechanic Lab release manifest

This checklist describes the v0.1.0 handoff and the commands that reproduce
the deliverables from a clean checkout. It is deliberately short enough to
use as a release gate.

## Included product surface

- Real event-driven 2D combat simulation with one-fight, 100-fight, and
  10,000-fight modes.
- Tk Combat Arena, Build/Skill/Enemy editors, Formula Lab, Replay, sweep and
  warning views.
- Data-driven JSON/YAML catalogues in `configs/`, plus the Clockwork Tyrant
  experiment specification in `demo/`.
- Safe arithmetic formula parser; formula text is never executed as Python.
- Batch metrics: win rate, DPS, mean/median/p95 TTK, damage taken, skill use,
  deaths, and event counts.
- Parameter sweeps and win-rate/TTK/damage-vs-HP heatmaps.
- Replays, experiment records, Markdown reports, automated tests, and source.

## Verification gate

Run from the project root:

```bash
python -m pytest -q
python -m compileall -q gamemechaniclab
python -m gamemechaniclab --demo --headless --runs 1 --output artifacts/release_smoke
python -m gamemechaniclab --headless --runs 10000
```

The first command is the release gate. The demo smoke run must write
`demo_results.json`, `balance_sweep.csv`, `replay_sample.json`, the primary
heatmaps, an experiment record, and a report. The final command is a bounded
single-build-versus-boss stress run; it keeps one replay sample rather than
retaining every fight in memory.

Reference verification for this handoff: `65 passed` in approximately 5.4 s; the
10,000-fight headless benchmark completed in approximately 123.0 s on the Linux
build host (10,000 wins, win rate 1.0, mean TTK 37.757 s, mean DPS 267.380,
mean damage taken 888.702).
The packaged Linux binary and the portable launcher each completed a one-fight smoke run
and emitted a canonical event replay.

The exact numbers above are refreshed from the final host run before each
handoff. Batch JSON also records `simulation_mode`: `event_replay` for the
replay-capable path and `accelerated_approximation` for high-volume batches
with replay disabled. The latter deliberately omits continuous position
traces and resolves projectile travel at the aggregate step; its explicit
exposure calibration is stored in `configs/mechanics.json`.

For a no-display machine, reproducible report screenshots can be regenerated
from the real demo data:

```bash
python docs/render_headless_screenshots.py \
  --input artifacts/final_demo \
  --output artifacts/screenshots
```

The live Tk screenshot helper is separate and requires a desktop display (or
`xvfb-run` on Linux):

```bash
python docs/capture_screenshots.py --output artifacts/screenshots
```

## Build outputs

Linux/macOS development machines can create the source-backed portable folder:

```bash
./build_portable.sh
dist/GameMechanicLab-portable/run.sh --headless --runs 1
```

The portable folder is not a Windows executable. Build the native Windows
deliverable on Windows:

```powershell
.\build_windows.ps1 -Clean
```

This produces `dist\GameMechanicLab.exe`. The script installs PyInstaller in
its local build environment and bundles `configs/`, `demo/`, and `docs/`.

## Artifact inventory

| Location | Purpose |
|---|---|
| `artifacts/final_demo/` | Generated 100-run reference metrics, sweep tables, heatmaps, replay, and report |
| `artifacts/screenshots/` | Reproducible headless dashboard and chart captures |
| `artifacts/formula_smoke_final/` | Paired Formula Lab variant JSON/CSV and replay smoke output |
| `dist/GameMechanicLab-source-demo.zip` | Source, configs, demo, docs, tests, and reference artifacts |
| `dist/GameMechanicLab-portable-linux.zip` | Portable source-backed launcher bundle |
| `dist/GameMechanicLab` | Linux single-file build when produced on Linux |
| `dist/GameMechanicLab.exe` | Native Windows build, produced by `build_windows.ps1` on Windows |

Generated caches, `build/`, and local virtual environments are not release
inputs. Keep the exact seed, configuration snapshot, run count, formula text,
and representative replay with any published balance conclusion.
