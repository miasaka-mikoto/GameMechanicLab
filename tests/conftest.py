"""Make source-checkout tests work with both ``pytest`` and ``python -m pytest``.

Some managed runners invoke the standalone pytest entrypoint without placing
the current working directory on ``sys.path``.  The project is intentionally
not installed for its smoke tests, so add the repository root explicitly.
"""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

