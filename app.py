"""Game Mechanic Lab launcher (desktop and reproducible headless modes)."""

from collections.abc import Sequence

from gamemechaniclab.cli import main as _main


def main(argv: Sequence[str] | None = None) -> int:
    return _main(list(argv) if argv is not None else None)


if __name__ == "__main__":
    raise SystemExit(main())
