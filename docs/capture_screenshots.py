#!/usr/bin/env python3
"""Capture a small UI smoke-test set for Game Mechanic Lab.

Run this on a desktop session (or under ``xvfb-run`` on Linux):

    python docs/capture_screenshots.py --output artifacts/screenshots

The script drives the real Tk application, runs a fight and a sweep, and saves
the visible window.  It intentionally uses Pillow only when available; the app
itself remains standard-library-only.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


def _grab(app, path: Path) -> bool:
    """Grab the root window using Pillow's ImageGrab backend."""
    try:
        from PIL import ImageGrab  # type: ignore
    except Exception:
        return False
    try:
        app.update_idletasks()
        app.update()
        x = app.winfo_rootx()
        y = app.winfo_rooty()
        w = app.winfo_width()
        h = app.winfo_height()
        image = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
        image.save(path)
        return True
    except Exception as exc:  # pragma: no cover - display/backend dependent
        print(f"capture failed for {path.name}: {exc}", file=sys.stderr)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/screenshots"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    try:
        from gamemechaniclab.ui.app import GameMechanicLabApp
    except Exception as exc:
        print(f"cannot import UI: {exc}", file=sys.stderr)
        return 2
    try:
        app = GameMechanicLabApp()
    except Exception as exc:  # no DISPLAY / Tk backend
        print("A graphical Tk session is required; run with a desktop or xvfb-run.", file=sys.stderr)
        print(f"details: {exc}", file=sys.stderr)
        return 2
    captured = 0
    try:
        app.update_idletasks()
        app.update()
        views = [("01_arena.png", "Arena")]
        for filename, page in views:
            try:
                app.show_page(page)
            except Exception:
                pass
            if _grab(app, args.output / filename):
                captured += 1

        # The same callbacks used by the buttons drive a real event loop and
        # populate the metrics/replay views before the screenshots are taken.
        try:
            app.run_simulation()
        except Exception as exc:
            print(f"simulation callback failed: {exc}", file=sys.stderr)
        if _grab(app, args.output / "02_simulation.png"):
            captured += 1

        try:
            app.show_page("Replay")
        except Exception:
            pass
        if _grab(app, args.output / "03_replay.png"):
            captured += 1

        try:
            app.run_sweep()
            app.update_idletasks()
            app.update()
        except Exception as exc:
            print(f"sweep callback failed: {exc}", file=sys.stderr)
        # A sweep is a transient Toplevel; capture the most recently-created
        # child when Tk exposes one, otherwise retain the root screenshot.
        windows = app.winfo_children()
        top = next((w for w in reversed(windows) if w.winfo_class() == "Toplevel"), app)
        if _grab(top, args.output / "04_sweep_heatmap.png"):
            captured += 1

        try:
            app.show_page("Formula")
        except Exception:
            pass
        if _grab(app, args.output / "05_formula_lab.png"):
            captured += 1
    finally:
        app.destroy()
    print(f"captured {captured} screenshot(s) in {args.output}")
    return 0 if captured else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
