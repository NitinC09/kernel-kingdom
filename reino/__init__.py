"""Kernel Kingdom: live wallpaper / idle game alimentado por tu sistema."""
__version__ = "0.3.2"

from . import compat as _compat

_compat.bootstrap()        # Windows: DLL de GTK y consola; en Linux no hace nada
