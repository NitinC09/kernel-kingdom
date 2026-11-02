"""Kernel Kingdom: live wallpaper / idle game fed by your system."""
__version__ = "0.4.0"

from . import compat as _compat

_compat.bootstrap()        # Windows: per-monitor DPI and console; does nothing on Linux
