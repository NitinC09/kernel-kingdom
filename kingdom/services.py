"""Starting with the session on Linux without systemd: OpenRC user services and XDG autostart.

  * OpenRC 0.60+ has user services (`rc-service --user`). The world runs as one, under
    supervise-daemon: restarted if it crashes, started with the user's OpenRC session.
  * The wallpaper needs the graphical session (WAYLAND_DISPLAY), which OpenRC filters out of
    user services and which doesn't exist yet when they start at login. It starts from the
    desktop's XDG autostart instead, exactly when the session is ready. On systems without
    OpenRC user services (older OpenRC, runit, s6...) autostart starts the world too.
"""
import os
import shutil
import subprocess
from pathlib import Path

SERVICE = "kernel-kingdom"
SCRIPT = """#!/sbin/openrc-run
description="kernel-kingdom: system monitor and kingdom simulation"
supervisor=supervise-daemon
command="{python}"
command_args="-m kingdom.daemon"
{directory}respawn_delay=5
output_log="{log}"
error_log="{log}"
"""
AUTOSTART = """[Desktop Entry]
Type=Application
Name=Kernel Kingdom
Comment=Starts Kernel Kingdom with the session
Exec={exec_line}
Icon=kernel-kingdom
Terminal=false
NoDisplay=true
X-GNOME-Autostart-enabled=true
"""


def _config() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def script_path() -> Path:
    return _config() / "rc" / "init.d" / SERVICE


def autostart_path() -> Path:
    return _config() / "autostart" / "kernel-kingdom.desktop"


def _rc(*args: str) -> bool:
    try:
        return subprocess.run(["rc-service", "--user", *args], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def openrc_init() -> bool:
    """OpenRC is the running init (it keeps its state in /run/openrc)."""
    return Path("/run/openrc").is_dir() and shutil.which("rc-service") is not None


def user_services() -> bool:
    """This OpenRC supports user services (0.60+) and the session has XDG_RUNTIME_DIR."""
    if not openrc_init() or not os.environ.get("XDG_RUNTIME_DIR"):
        return False
    try:                                  # `--list` fails when there are no user services yet: ask the help
        out = subprocess.run(["rc-service", "--help"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return "--user" in out.stdout + out.stderr


def installed() -> bool:
    return script_path().exists()


def install(python: str, workdir: str | None, log: Path) -> bool:
    """Writes the world's user service, adds it to the user's default runlevel and starts it."""
    path = script_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    (_config() / "rc" / "runlevels" / "default").mkdir(parents=True, exist_ok=True)
    path.write_text(SCRIPT.format(python=python, log=log,
                                  directory=f'directory="{workdir}"\n' if workdir else ""), encoding="utf-8")
    path.chmod(0o755)
    try:
        subprocess.run(["rc-update", "--user", "add", SERVICE, "default"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass
    return start()


def start() -> bool:
    return installed() and _rc(SERVICE, "start")


def stop() -> bool:
    return installed() and _rc(SERVICE, "stop")


def restart() -> bool:
    return installed() and _rc(SERVICE, "restart")


def uninstall():
    if not installed():
        return
    stop()
    try:
        subprocess.run(["rc-update", "--user", "del", SERVICE, "default"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass
    script_path().unlink(missing_ok=True)


def write_autostart(exec_line: str):
    path = autostart_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(AUTOSTART.format(exec_line=exec_line), encoding="utf-8")


def remove_autostart():
    autostart_path().unlink(missing_ok=True)
