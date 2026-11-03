"""Differences between operating systems, all in one place.

Linux is the usual path (nothing changes there). On Windows:
  * there is no systemd, Wayland, /proc or /dev/input: the daemon uses psutil and
    GetLastInputInfo (presence), autostart is a shortcut in the Startup folder
    and the launcher goes to the Start menu;
  * os.kill(pid, 0) *kills* the process, so whether a pid is alive is checked with psutil;
  * os.execv doesn't really replace the process: a child is started and waited for.
"""
import os
import subprocess
import sys
from pathlib import Path

WINDOWS = sys.platform == "win32"
LINUX = sys.platform.startswith("linux")
WALLPAPER = LINUX or WINDOWS   # Linux: Wayland layer-shell. Windows: Explorer's WorkerW layer


def bootstrap():
    """Windows: real pixel coordinates on every monitor, and keep ✔ ✘ and accented letters
    from breaking a cp1252 console."""
    if not WINDOWS:
        return
    _dpi_aware()
    for stream in (sys.stdout, sys.stderr):          # ✔ ✘ and accents on a cp1252 console
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def _dpi_aware():
    """Per-monitor v2, like Explorer. Without it Windows gives Python scaled coordinates
    (a monitor at 125% measures 1536x864 instead of 1920x1080) and the wallpaper lands in the
    wrong place on setups with different scales."""
    import ctypes
    user32 = ctypes.windll.user32
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):    # PER_MONITOR_AWARE_V2
            return
    except AttributeError:                                               # Windows 10 anterior a 1703
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)                   # PROCESS_PER_MONITOR_DPI_AWARE
    except (AttributeError, OSError):
        try:
            user32.SetProcessDPIAware()
        except AttributeError:
            pass


def pid_alive(pid: int) -> bool:
    if WINDOWS:
        import psutil
        try:
            return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
        except psutil.Error:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def terminate(pid: int):
    if WINDOWS:
        import psutil
        try:
            psutil.Process(pid).terminate()
        except psutil.Error:
            pass
        return
    import signal
    os.kill(pid, signal.SIGTERM)


def detach_kwargs() -> dict:
    """Popen(...) detached from the terminal: a new session on POSIX; on Windows, no console
    and its own process group (closing the terminal doesn't kill it)."""
    if WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def exec_python(*args: str):
    """Replace this process with `python args...` (on Windows: run it and exit with its code)."""
    argv = [sys.executable, *args]
    if not WINDOWS:
        os.execv(sys.executable, argv)
    sys.exit(subprocess.call(argv))


def lower_priority(nice: int):
    """Linux: nice (the daemon never competes with your processes). On Windows it is NOT lowered:
    with the CPU at 100% the scheduler barely runs a BELOW_NORMAL process, and the world froze
    right when it had to react (storms, eruptions). Sampling is cheap."""
    if hasattr(os, "nice"):
        try:
            os.nice(nice)
        except OSError:
            pass


def idle_seconds() -> float | None:
    """Seconds without keyboard or mouse (Windows: GetLastInputInfo). None if unknown."""
    if not WINDOWS:
        return None
    import ctypes
    from ctypes import wintypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return None
    now = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
    return ((now - info.dwTime) & 0xFFFFFFFF) / 1000


# -- Windows: Start menu and autostart --------------------------------------------------

def _appdata() -> Path:
    return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")


def start_menu() -> Path:
    return _appdata() / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def startup_folder() -> Path:
    return start_menu() / "Startup"


def gui_python() -> str:
    """pythonw.exe (no console window) if it exists next to the interpreter."""
    w = Path(sys.executable).with_name("pythonw.exe")
    return str(w) if w.exists() else sys.executable


def png_to_ico(png: Path, ico: Path):
    """An .ico with the PNG inside (valid since Vista): no Pillow needed."""
    import struct
    data = png.read_bytes()
    w, h = struct.unpack(">II", data[16:24])
    ico.write_bytes(struct.pack("<HHH", 0, 1, 1)
                    + struct.pack("<BBBBHHII", w % 256, h % 256, 0, 0, 1, 32, len(data), 22) + data)


def make_shortcut(lnk: Path, target: str, args: str = "", icon: str = "", workdir: str = "", desc: str = ""):
    """.lnk shortcut via WScript.Shell (ships with every Windows), through PowerShell."""
    def q(s: str) -> str:
        return "'" + str(s).replace("'", "''") + "'"
    lnk.parent.mkdir(parents=True, exist_ok=True)
    script = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut({q(lnk)});"
              f"$s.TargetPath={q(target)};$s.Arguments={q(args)};$s.Description={q(desc)};"
              + (f"$s.IconLocation={q(icon)};" if icon else "")
              + (f"$s.WorkingDirectory={q(workdir)};" if workdir else "")
              + "$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                   check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)


def pictures_dir() -> Path:
    """The user's Pictures folder (on Windows it may be in OneDrive or moved)."""
    if WINDOWS:
        import ctypes
        import uuid
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("data", ctypes.c_byte * 16)]
        guid = GUID()
        ctypes.memmove(guid.data, uuid.UUID("33E28130-4E1E-4676-835A-98395C3BC3BB").bytes_le, 16)
        out = ctypes.c_wchar_p()
        fn = ctypes.windll.shell32.SHGetKnownFolderPath
        fn.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
        if fn(ctypes.byref(guid), 0, None, ctypes.byref(out)) == 0 and out.value:
            path = Path(out.value)
            ctypes.windll.ole32.CoTaskMemFree(out)
            return path
    try:
        out = subprocess.run(["xdg-user-dir", "PICTURES"], capture_output=True, text=True, timeout=2).stdout.strip()
        if out and out != str(Path.home()):
            return Path(out)
    except (OSError, subprocess.SubprocessError):
        pass
    return Path.home() / "Pictures"
