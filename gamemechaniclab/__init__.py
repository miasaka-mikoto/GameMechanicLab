"""Game Mechanic Lab - data-driven combat balance experiments.

The package intentionally keeps the UI dependency-free (Tkinter + Python
standard library).  The simulator can be supplied by :mod:`gamemechaniclab`
or by a compatible project module; the UI falls back to a deterministic demo
engine so it remains useful while the core is being developed.
"""

__version__ = "0.1.0"

# Keep the documented ``from gamemechaniclab import CombatSimulator`` API
# available while the desktop UI remains an optional layer.
from .core import *  # noqa: F401,F403,E402
