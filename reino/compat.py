"""Diferencias entre sistemas operativos, todas en un solo lugar.

Linux es el camino de siempre (nada cambia ahí). En Windows:
  * no hay systemd, Wayland, /proc ni /dev/input: el daemon usa psutil y
    GetLastInputInfo (presencia), el arranque automático es un acceso directo
    en la carpeta Inicio y el lanzador va al menú Inicio;
  * os.kill(pid, 0) *mata* el proceso, así que la vida de un pid se mira con psutil;
  * os.execv no reemplaza el proceso de verdad: se lanza un hijo y se espera.
"""
import os
import subprocess
import sys
from pathlib import Path

WINDOWS = sys.platform == "win32"
LINUX = sys.platform.startswith("linux")
WALLPAPER = LINUX or WINDOWS   # Linux: layer-shell de Wayland. Windows: la capa WorkerW de Explorer


def bootstrap():
    """Windows: coordenadas en píxeles reales en todos los monitores, y que ✔ ✘ y las tildes
    no rompan una consola cp1252."""
    if not WINDOWS:
        return
    _dpi_aware()
    for stream in (sys.stdout, sys.stderr):          # ✔ ✘ y tildes en la consola cp1252
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def _dpi_aware():
    """Per-monitor v2, como Explorer. Sin esto Windows le da a Python coordenadas escaladas
    (un monitor al 125% mide 1536x864 en vez de 1920x1080) y el fondo cae fuera de lugar en
    configuraciones con escalas distintas."""
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
    """Popen(...) separado de la terminal: nueva sesión en POSIX; en Windows, sin consola
    y en su propio grupo (cerrar la terminal no lo mata)."""
    if WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def exec_python(*args: str):
    """Reemplaza este proceso por `python args...` (en Windows: lo corre y sale con su código)."""
    argv = [sys.executable, *args]
    if not WINDOWS:
        os.execv(sys.executable, argv)
    sys.exit(subprocess.call(argv))


def lower_priority(nice: int):
    if hasattr(os, "nice"):
        try:
            os.nice(nice)
        except OSError:
            pass
        return
    try:
        import psutil
        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if nice > 0 else psutil.NORMAL_PRIORITY_CLASS)
    except Exception:
        pass


def idle_seconds() -> float | None:
    """Segundos sin teclado ni mouse (Windows: GetLastInputInfo). None si no se puede saber."""
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


# -- Windows: menú Inicio y arranque automático ------------------------------------------

def _appdata() -> Path:
    return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")


def start_menu() -> Path:
    return _appdata() / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def startup_folder() -> Path:
    return start_menu() / "Startup"


def gui_python() -> str:
    """pythonw.exe (sin ventana de consola) si existe al lado del intérprete."""
    w = Path(sys.executable).with_name("pythonw.exe")
    return str(w) if w.exists() else sys.executable


def png_to_ico(png: Path, ico: Path):
    """Un .ico con el PNG adentro (válido desde Vista): así no hace falta Pillow."""
    import struct
    data = png.read_bytes()
    w, h = struct.unpack(">II", data[16:24])
    ico.write_bytes(struct.pack("<HHH", 0, 1, 1)
                    + struct.pack("<BBBBHHII", w % 256, h % 256, 0, 0, 1, 32, len(data), 22) + data)


def make_shortcut(lnk: Path, target: str, args: str = "", icon: str = "", workdir: str = "", desc: str = ""):
    """Acceso directo .lnk con WScript.Shell (viene con todo Windows), vía PowerShell."""
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
    """Carpeta Imágenes del usuario (en Windows puede estar en OneDrive o movida)."""
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
