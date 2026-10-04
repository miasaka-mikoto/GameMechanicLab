"""Launch the bundled Game Mechanic Lab demo.

The launcher keeps the same behavior in a source checkout and in the portable
folder produced by build_portable.sh.  ``--demo`` is passed to app.py when the
application exposes command-line mode; older app revisions that do not accept
arguments are still supported by retrying without the flag.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parent
    app_path = root / "app.py"
    if not app_path.exists():
        raise FileNotFoundError(f"Game Mechanic Lab entrypoint not found: {app_path}")

    original_argv = sys.argv[:]
    try:
        sys.argv = [str(app_path), "--demo", *original_argv[1:]]
        try:
            runpy.run_path(str(app_path), run_name="__main__")
        except SystemExit as exc:
            # argparse uses SystemExit for an unknown optional flag. Retry the
            # default launch so the portable demo remains usable across app
            # revisions; preserve real non-zero failures.
            if exc.code == 0:
                return
            if exc.code != 2:
                raise
            sys.argv = [str(app_path), *original_argv[1:]]
            runpy.run_path(str(app_path), run_name="__main__")
    finally:
        sys.argv = original_argv


if __name__ == "__main__":
    main()
