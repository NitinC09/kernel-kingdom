"""Fondo de pantalla en Windows: la ventana GTK se cuelga de la capa del escritorio de Explorer.

Es el truco de Wallpaper Engine / Lively: el mensaje 0x052C le pide a Progman que cree una
ventana WorkerW entre el fondo y los íconos; nuestra ventana pasa a ser hija de esa WorkerW,
así queda debajo de los íconos (los clics van al escritorio) y encima de la imagen del fondo.

  * Windows 10 / 11 hasta 23H2: la WorkerW que importa es la *siguiente* a la que contiene
    SHELLDLL_DefView (los íconos).
  * Windows 11 24H2+: Progman contiene a SHELLDLL_DefView y a una WorkerW hija: esa.

Pausa (oclusión): una ventana maximizada, en pantalla completa o que tapa ≥ coverage del
monitor -> sin frames. Se consulta una vez por segundo (EnumWindows cuesta ~1 ms).

Si Explorer se reinicia, la WorkerW muere y con ella nuestra ventana: el renderer lo nota
(IsWindow) y se relanza solo. Todo es ctypes: nada que instalar.
"""
import os
import sys

def _cov(win: tuple[int, int, int, int], mon: tuple[int, int, int, int]) -> float:
    """Fracción del monitor (l, t, r, b) que tapa la ventana (l, t, r, b)."""
    w = max(0, min(win[2], mon[2]) - max(win[0], mon[0]))
    h = max(0, min(win[3], mon[3]) - max(win[1], mon[1]))
    area = (mon[2] - mon[0]) * (mon[3] - mon[1])
    return w * h / area if area > 0 else 0.0


if sys.platform == "win32":                      # en otros sistemas solo sirve _cov (para los tests)
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    dwmapi = ctypes.WinDLL("dwmapi")
    LONG_PTR = ctypes.c_ssize_t
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    MONITORENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                         ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]

    def _sig(fn, res, *args):
        f = getattr(user32, fn)
        f.restype, f.argtypes = res, list(args)
        return f

    H, B, I, U = wintypes.HWND, wintypes.BOOL, ctypes.c_int, wintypes.UINT
    FindWindowW = _sig("FindWindowW", H, wintypes.LPCWSTR, wintypes.LPCWSTR)
    FindWindowExW = _sig("FindWindowExW", H, H, H, wintypes.LPCWSTR, wintypes.LPCWSTR)
    SendMessageTimeoutW = _sig("SendMessageTimeoutW", wintypes.LPARAM, H, U, wintypes.WPARAM, wintypes.LPARAM,
                               U, U, ctypes.POINTER(ctypes.c_size_t))
    EnumWindows = _sig("EnumWindows", B, WNDENUMPROC, wintypes.LPARAM)
    SetParent = _sig("SetParent", H, H, H)
    GetAncestor = _sig("GetAncestor", H, H, U)
    _getlong = "GetWindowLongPtrW" if hasattr(user32, "GetWindowLongPtrW") else "GetWindowLongW"
    _setlong = "SetWindowLongPtrW" if hasattr(user32, "SetWindowLongPtrW") else "SetWindowLongW"
    GetWindowLong = _sig(_getlong, LONG_PTR, H, I)
    SetWindowLong = _sig(_setlong, LONG_PTR, H, I, LONG_PTR)
    SetWindowPos = _sig("SetWindowPos", B, H, H, I, I, I, I, U)
    IsWindow = _sig("IsWindow", B, H)
    IsWindowVisible = _sig("IsWindowVisible", B, H)
    IsIconic = _sig("IsIconic", B, H)
    IsZoomed = _sig("IsZoomed", B, H)
    GetWindowRect = _sig("GetWindowRect", B, H, ctypes.POINTER(wintypes.RECT))
    GetClassNameW = _sig("GetClassNameW", I, H, wintypes.LPWSTR, I)
    GetWindowThreadProcessId = _sig("GetWindowThreadProcessId", wintypes.DWORD, H, ctypes.POINTER(wintypes.DWORD))
    ScreenToClient = _sig("ScreenToClient", B, H, ctypes.POINTER(wintypes.POINT))
    EnumDisplayMonitors = _sig("EnumDisplayMonitors", B, wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                               MONITORENUMPROC, wintypes.LPARAM)
    GetMonitorInfoW = _sig("GetMonitorInfoW", B, wintypes.HMONITOR, ctypes.POINTER(MONITORINFOEXW))
    SystemParametersInfoW = _sig("SystemParametersInfoW", B, U, U, ctypes.c_void_p, U)
    dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
    dwmapi.DwmGetWindowAttribute.argtypes = [H, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]

GWL_STYLE, GWL_EXSTYLE = -16, -20
WS_CHILD, WS_VISIBLE, WS_POPUP = 0x40000000, 0x10000000, 0x80000000
WS_FRAME = 0x00C00000 | 0x00040000 | 0x00080000 | 0x00020000 | 0x00010000   # caption, borde, menú, min/max
WS_EX_TOOLWINDOW, WS_EX_APPWINDOW, WS_EX_NOACTIVATE = 0x80, 0x40000, 0x08000000
SWP_NOZORDER, SWP_NOACTIVATE, SWP_FRAMECHANGED, SWP_SHOWWINDOW = 0x4, 0x10, 0x20, 0x40
GA_PARENT, SMTO_NORMAL, DWMWA_CLOAKED, HWND_BOTTOM = 1, 0, 14, 1
SHELL = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "SHELLDLL_DefView",
         "NotifyIconOverflowWindow", "Windows.UI.Core.CoreWindow", "XamlExplorerHostIslandWindow"}


def monitors() -> list[dict]:
    """Monitores en píxeles físicos: el principal primero, después de izquierda a derecha."""
    out = []

    def cb(hmon, hdc, rect, data):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if GetMonitorInfoW(hmon, ctypes.byref(info)):
            r = info.rcMonitor
            out.append({"rect": (r.left, r.top, r.right, r.bottom), "device": info.szDevice,
                        "primary": bool(info.dwFlags & 1)})
        return True
    EnumDisplayMonitors(None, None, MONITORENUMPROC(cb), 0)
    out.sort(key=lambda m: (not m["primary"], m["rect"][0], m["rect"][1]))
    return out


def _class(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    GetClassNameW(hwnd, buf, 256)
    return buf.value


def _rect(hwnd) -> tuple[int, int, int, int]:
    r = wintypes.RECT()
    GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def _pid(hwnd) -> int:
    pid = wintypes.DWORD()
    GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def desktop_layer():
    """HWND de la WorkerW detrás de los íconos (None si no hay Explorer)."""
    progman = FindWindowW("Progman", None)
    if not progman:
        return None
    res = ctypes.c_size_t()
    for wp, lp in ((0xD, 0x1), (0, 0)):            # distintas versiones de Explorer escuchan uno u otro
        SendMessageTimeoutW(progman, 0x052C, wp, lp, SMTO_NORMAL, 1000, ctypes.byref(res))
    if FindWindowExW(progman, None, "SHELLDLL_DefView", None):     # 24H2+: todo cuelga de Progman
        return FindWindowExW(progman, None, "WorkerW", None) or progman
    found = []

    def cb(hwnd, _):
        if FindWindowExW(hwnd, None, "SHELLDLL_DefView", None):
            found.append(FindWindowExW(None, hwnd, "WorkerW", None))
            return False
        return True
    EnumWindows(WNDENUMPROC(cb), 0)
    return (found and found[0]) or progman


def refresh_wallpaper():
    """Vuelve a pintar el fondo de Windows (tras sacar el nuestro, para no dejar un cuadro viejo)."""
    buf = ctypes.create_unicode_buffer(520)
    if SystemParametersInfoW(0x73, 520, buf, 0):                    # SPI_GETDESKWALLPAPER
        SystemParametersInfoW(0x14, 0, buf, 0x2)                    # SPI_SETDESKWALLPAPER, SPIF_SENDCHANGE


class Occlusion:
    """Misma interfaz que render.occlusion.OcclusionWatcher (covered, recompute), consultada con poll()."""

    def __init__(self, rect: tuple[int, int, int, int], coverage: float):
        self.rect, self.coverage = rect, coverage
        self.covered = False
        self.output_name = self.output_size = None
        self._me = os.getpid()

    def recompute(self) -> bool:
        return False

    def poll(self) -> bool:
        before = self.covered
        self.covered = self._compute()
        return self.covered != before

    def _compute(self) -> bool:
        hit = []
        cx, cy = (self.rect[0] + self.rect[2]) // 2, (self.rect[1] + self.rect[3]) // 2

        def cb(hwnd, _):
            if not IsWindowVisible(hwnd) or IsIconic(hwnd) or _pid(hwnd) == self._me:
                return True
            if GetWindowLong(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW or _class(hwnd) in SHELL:
                return True
            cloaked = wintypes.DWORD()                 # apps UWP ocultas y ventanas de otro escritorio virtual
            if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), 4) == 0 and cloaked.value:
                return True
            r = _rect(hwnd)
            if (IsZoomed(hwnd) and r[0] <= cx < r[2] and r[1] <= cy < r[3]) or _cov(r, self.rect) >= self.coverage:
                hit.append(hwnd)
                return False
            return True
        EnumWindows(WNDENUMPROC(cb), 0)
        return bool(hit)


class DesktopHost:
    """La ventana de un monitor colgada de la capa del escritorio."""

    def __init__(self, monitor: str | None):
        mons = monitors()
        idx = int(monitor) if monitor and monitor.isdigit() else 0
        if idx >= len(mons):
            raise OSError(f"no hay monitor {idx}")
        self.index, self.rect = idx, mons[idx]["rect"]
        self.title = f"kernel-kingdom-wallpaper-{os.getpid()}-{idx}"
        self.hwnd = self.layer = None

    @property
    def size(self) -> tuple[int, int]:
        return self.rect[2] - self.rect[0], self.rect[3] - self.rect[1]

    @property
    def logical_size(self) -> tuple[int, int]:
        """Tamaño en píxeles de GTK (escala de pantalla del sistema), para el primer frame."""
        try:
            scale = user32.GetDpiForSystem() / 96 or 1
        except AttributeError:                       # Windows anterior a 10 1607
            scale = 1
        return int(self.size[0] / scale), int(self.size[1] / scale)

    def alive(self) -> bool:
        return bool(self.hwnd and IsWindow(self.hwnd))

    def occlusion(self, coverage: float) -> Occlusion:
        return Occlusion(self.rect, coverage)

    def attach(self):
        hwnd = FindWindowW(None, self.title)
        if not hwnd or _pid(hwnd) != os.getpid():
            raise OSError("la ventana todavía no existe")
        layer = desktop_layer()
        if not layer:
            raise OSError("no se encontró el escritorio de Explorer")
        style = GetWindowLong(hwnd, GWL_STYLE) & 0xFFFFFFFF
        SetWindowLong(hwnd, GWL_STYLE, (style & ~(WS_POPUP | WS_FRAME)) | WS_CHILD | WS_VISIBLE)
        ex = GetWindowLong(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
        SetWindowLong(hwnd, GWL_EXSTYLE, (ex & ~WS_EX_APPWINDOW) | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        ctypes.set_last_error(0)
        if not SetParent(hwnd, layer) and ctypes.get_last_error():
            raise OSError(f"SetParent falló (error {ctypes.get_last_error()})")
        if GetAncestor(hwnd, GA_PARENT) != layer:
            raise OSError("la ventana no quedó dentro de la capa del escritorio")
        self.hwnd, self.layer = hwnd, layer
        self.on_progman = _class(layer) == "Progman"   # sin WorkerW: debajo de los íconos a mano
        self._place()

    def describe(self) -> str:
        """Para el log: dónde quedó la ventana respecto de dónde debía."""
        return (f"monitor {self.index} {self.rect}, capa {_class(self.layer)} "
                f"{_rect(self.layer)}, ventana {_rect(self.hwnd)}")

    def _place(self):
        pt = wintypes.POINT(self.rect[0], self.rect[1])          # coordenadas de pantalla -> de la WorkerW
        ScreenToClient(self.layer, ctypes.byref(pt))
        w, h = self.size
        flags = SWP_NOACTIVATE | SWP_FRAMECHANGED | SWP_SHOWWINDOW
        SetWindowPos(self.hwnd, HWND_BOTTOM if self.on_progman else None, pt.x, pt.y, w, h,
                     flags if self.on_progman else flags | SWP_NOZORDER)

    def check(self) -> str:
        """'ok', 'lost' (Explorer se reinició y nos llevó puestos) o 'gone' (ya no está el monitor)."""
        if not self.alive():
            return "lost"
        mons = monitors()
        if self.index >= len(mons):
            return "gone"
        rect = mons[self.index]["rect"]
        if GetAncestor(self.hwnd, GA_PARENT) != self.layer or not IsWindow(self.layer):
            self.rect = rect
            self.attach()
        elif rect != self.rect or _rect(self.hwnd) != rect:       # cambió la resolución o GTK la movió
            self.rect = rect
            self._place()
        return "ok"
