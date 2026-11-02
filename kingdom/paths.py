"""XDG paths shared by daemon and renderer (on Windows: %APPDATA%, %LOCALAPPDATA%, %TEMP%)."""
import os
import tempfile
from pathlib import Path

from .compat import WINDOWS

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = Path(__file__).resolve().parent / "default_config.toml"


# Windows: an XDG variable wins if someone set it; otherwise native folders
# (config in %APPDATA%\kernel-kingdom, world and data in %LOCALAPPDATA%\kernel-kingdom).
_WIN = {"XDG_CONFIG_HOME": ("APPDATA", "Roaming"), "XDG_STATE_HOME": ("LOCALAPPDATA", "Local"),
        "XDG_DATA_HOME": ("LOCALAPPDATA", "Local")}


def _xdg(var: str, fallback: str) -> Path:
    if os.environ.get(var):
        return Path(os.environ[var])
    if WINDOWS and var in _WIN:
        env, sub = _WIN[var]
        return Path(os.environ.get(env) or Path.home() / "AppData" / sub)
    return Path.home() / fallback


def user_config() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "kernel-kingdom" / "config.toml"


def state_file() -> Path:
    """Live state: goes to XDG_RUNTIME_DIR (tmpfs), so we don't wear the disk every 2 s."""
    runtime = os.environ.get("XDG_RUNTIME_DIR") or (
        tempfile.gettempdir() if WINDOWS else f"/tmp/kernel-kingdom-{os.getuid()}")
    return Path(runtime) / "kernel-kingdom" / "state.json"


def commands_dir() -> Path:
    """Power requests from the wallpaper to the daemon: one JSON file per request."""
    return state_file().parent / "commands"


def save_file() -> Path:
    """World that persists across restarts."""
    return _xdg("XDG_STATE_HOME", ".local/state") / "kernel-kingdom" / "world.json"


def migrate_legacy():
    """The project used to be called lw-reino: if there is a world or a config under the
    old name and none yet under the new one, they are copied (the kingdom isn't lost)."""
    import shutil
    for new, folder, base in ((save_file(), "XDG_STATE_HOME", ".local/state"),
                              (user_config(), "XDG_CONFIG_HOME", ".config")):
        old = _xdg(folder, base) / "lw-reino" / new.name
        if old.exists() and not new.exists():
            new.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old, new)


def data_dir() -> Path:
    """Daily chronicle archive and exported stories."""
    return _xdg("XDG_DATA_HOME", ".local/share") / "kernel-kingdom"
