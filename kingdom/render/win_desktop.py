"""Wallpaper on Windows: the window hangs from Explorer's desktop layer.

It's the Wallpaper Engine / Lively trick: message 0x052C asks Progman to create a
WorkerW window between the background and the icons; our window becomes a child of that
WorkerW, so it sits below the icons (clicks go to the desktop) and above the wallpaper image.

  * Windows 10 / 11 up to 23H2: the WorkerW that matters is the one *after* the one that
    contains SHELLDLL_DefView (the icons).
  * Windows 11 24H2+: Progman contains SHELLDLL_DefView and a child WorkerW: that one.

Pause (occlusion), like Lively: a MAXIMIZED window on that monitor, or the ACTIVE window
covering >= coverage of it (fullscreen: games, videos) -> no frames. Fullscreen overlays
(NVIDIA, Steam, game bars) are never maximized or active, so they don't freeze it.
Checked once per second (EnumWindows costs ~1 ms).

If Explorer restarts, the WorkerW dies and our window with it: the renderer notices
(IsWindow) and relaunches itself. It's all ctypes: nothing to install.
"""
import os
import sys

def _cov(win: tuple[int, int, int, int], mon: tuple[int, int, int, int]) -> float:
    """Fraction of the monitor (l, t, r, b) covered by the window (l, t, r, b)."""
    w = max(0, min(win[2], mon[2]) - max(win[0], mon[0]))
    h = max(0, min(win[3], mon[3]) - max(win[1], mon[1]))
    area = (mon[2] - mon[0]) * (mon[3] - mon[1])
    return w * h / area if area > 0 else 0.0


if sys.platform == "win32":                      # on other systems only _cov is used (by the tests)
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
    GetWindowTextW = _sig("GetWindowTextW", I, H, wintypes.LPWSTR, I)
    GetForegroundWindow = _sig("GetForegroundWindow", H)
    GetLayeredWindowAttributes = _sig("GetLayeredWindowAttributes", B, H, ctypes.POINTER(wintypes.DWORD),
                                      ctypes.POINTER(ctypes.c_ubyte), ctypes.POINTER(wintypes.DWORD))
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
WS_FRAME = 0x00C00000 | 0x00040000 | 0x00080000 | 0x00020000 | 0x00010000   # caption, border, menu, min/max
WS_EX_TOOLWINDOW, WS_EX_APPWINDOW, WS_EX_NOACTIVATE = 0x80, 0x40000, 0x08000000
WS_EX_TRANSPARENT, WS_EX_LAYERED, LWA_ALPHA = 0x20, 0x80000, 0x2
# Fullscreen overlays that cover nothing (NVIDIA, game bars, Steam...): without this
# the wallpaper paused itself a second after starting.
OVERLAY_CLASSES = {"CEF-OSC-WIDGET", "GameOverlayUI", "XboxGameBarWidget", "MSCTFIME UI", "IME"}
SWP_NOZORDER, SWP_NOACTIVATE, SWP_FRAMECHANGED, SWP_SHOWWINDOW = 0x4, 0x10, 0x20, 0x40
GA_PARENT, SMTO_NORMAL, DWMWA_CLOAKED, HWND_BOTTOM = 1, 0, 14, 1
SHELL = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "SHELLDLL_DefView",
         "NotifyIconOverflowWindow", "Windows.UI.Core.CoreWindow", "XamlExplorerHostIslandWindow"}


def monitors() -> list[dict]:
    """Monitors in physical pixels: the primary first, then left to right."""
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


def _title(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    GetWindowTextW(hwnd, buf, 256)
    return buf.value


def _see_through(hwnd) -> bool:
    """Layered window with transparency (alpha < 255): an overlay, it doesn't cover the wallpaper."""
    key, alpha, flags = wintypes.DWORD(), ctypes.c_ubyte(255), wintypes.DWORD()
    if not GetLayeredWindowAttributes(hwnd, ctypes.byref(key), ctypes.byref(alpha), ctypes.byref(flags)):
        return False
    return bool(flags.value & LWA_ALPHA) and alpha.value < 255


def _rect(hwnd) -> tuple[int, int, int, int]:
    r = wintypes.RECT()
    GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def _pid(hwnd) -> int:
    pid = wintypes.DWORD()
    GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def desktop_layer():
    """HWND of the WorkerW behind the icons (None if there's no Explorer)."""
    progman = FindWindowW("Progman", None)
    if not progman:
        return None
    res = ctypes.c_size_t()
    for wp, lp in ((0xD, 0x1), (0, 0)):            # different Explorer versions listen to one or the other
        SendMessageTimeoutW(progman, 0x052C, wp, lp, SMTO_NORMAL, 1000, ctypes.byref(res))
    if FindWindowExW(progman, None, "SHELLDLL_DefView", None):     # 24H2+: everything hangs from Progman
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
    """Repaints the Windows wallpaper (after removing ours, so no stale frame is left)."""
    buf = ctypes.create_unicode_buffer(520)
    if SystemParametersInfoW(0x73, 520, buf, 0):                    # SPI_GETDESKWALLPAPER
        SystemParametersInfoW(0x14, 0, buf, 0x2)                    # SPI_SETDESKWALLPAPER, SPIF_SENDCHANGE


class Occlusion:
    """Same interface as render.occlusion.OcclusionWatcher (covered, recompute), queried with poll()."""

    def __init__(self, rect: tuple[int, int, int, int], coverage: float):
        self.rect, self.coverage = rect, coverage
        self.covered = False
        self.culprit = None                          # which window covers it (for the log)
        self.output_name = self.output_size = None
        self._me = os.getpid()

    def recompute(self) -> bool:
        return False

    def poll(self) -> bool:
        before = self.covered
        self.covered = self._compute()
        return self.covered != before

    def _counts(self, hwnd) -> bool:
        """Is it a real application window (not an overlay, a tool or the desktop)?"""
        if not hwnd or not IsWindowVisible(hwnd) or IsIconic(hwnd) or _pid(hwnd) == self._me:
            return False
        ex = GetWindowLong(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
        if ex & (WS_EX_TOOLWINDOW | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE):
            return False
        cls = _class(hwnd)
        if cls in SHELL or cls in OVERLAY_CLASSES or not _title(hwnd):
            return False
        if ex & WS_EX_LAYERED and _see_through(hwnd):
            return False
        cloaked = wintypes.DWORD()                     # hidden UWP apps and windows on another virtual desktop
        return not (dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), 4) == 0
                    and cloaked.value)

    def _compute(self) -> bool:
        hit = []
        cx, cy = (self.rect[0] + self.rect[2]) // 2, (self.rect[1] + self.rect[3]) // 2
        fg = GetForegroundWindow()                     # fullscreen: only the active window
        if self._counts(fg) and _cov(_rect(fg), self.rect) >= self.coverage:
            self.culprit = f"{_class(fg)} '{_title(fg)}' (activa)"
            return True

        def cb(hwnd, _):
            if not IsZoomed(hwnd) or not self._counts(hwnd):
                return True
            r = _rect(hwnd)
            if r[0] <= cx < r[2] and r[1] <= cy < r[3]:   # maximized on THIS monitor
                hit.append(hwnd)
                return False
            return True
        EnumWindows(WNDENUMPROC(cb), 0)
        self.culprit = f"{_class(hit[0])} '{_title(hit[0])}' (maximizada)" if hit else None
        return bool(hit)


class DesktopHost:
    """The window of one monitor hanging from the desktop layer."""

    def __init__(self, monitor: str | None):
        mons = monitors()
        idx = int(monitor) if monitor and monitor.isdigit() else 0
        if idx >= len(mons):
            raise OSError(f"no monitor {idx}")
        self.index, self.rect = idx, mons[idx]["rect"]
        self.title = f"kernel-kingdom-wallpaper-{os.getpid()}-{idx}"
        self.hwnd = self.layer = None

    @property
    def size(self) -> tuple[int, int]:
        return self.rect[2] - self.rect[0], self.rect[3] - self.rect[1]

    def alive(self) -> bool:
        return bool(self.hwnd and IsWindow(self.hwnd))

    def occlusion(self, coverage: float) -> Occlusion:
        return Occlusion(self.rect, coverage)

    def attach(self):
        hwnd = FindWindowW(None, self.title)
        if not hwnd or _pid(hwnd) != os.getpid():
            raise OSError("the window doesn't exist yet")
        layer = desktop_layer()
        if not layer:
            raise OSError("Explorer's desktop wasn't found")
        style = GetWindowLong(hwnd, GWL_STYLE) & 0xFFFFFFFF
        SetWindowLong(hwnd, GWL_STYLE, (style & ~(WS_POPUP | WS_FRAME)) | WS_CHILD | WS_VISIBLE)
        ex = GetWindowLong(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
        SetWindowLong(hwnd, GWL_EXSTYLE, (ex & ~WS_EX_APPWINDOW) | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        ctypes.set_last_error(0)
        if not SetParent(hwnd, layer) and ctypes.get_last_error():
            raise OSError(f"SetParent failed (error {ctypes.get_last_error()})")
        if GetAncestor(hwnd, GA_PARENT) != layer:
            raise OSError("the window didn't end up inside the desktop layer")
        self.hwnd, self.layer = hwnd, layer
        self.on_progman = _class(layer) == "Progman"   # no WorkerW: below the icons by hand
        self._place()

    def describe(self) -> str:
        """For the log: where the window ended up compared to where it should be."""
        return (f"monitor {self.index} {self.rect}, capa {_class(self.layer)} "
                f"{_rect(self.layer)}, window {_rect(self.hwnd)}")

    def _place(self):
        pt = wintypes.POINT(self.rect[0], self.rect[1])          # screen coordinates -> WorkerW coordinates
        ScreenToClient(self.layer, ctypes.byref(pt))
        w, h = self.size
        flags = SWP_NOACTIVATE | SWP_FRAMECHANGED | SWP_SHOWWINDOW
        SetWindowPos(self.hwnd, HWND_BOTTOM if self.on_progman else None, pt.x, pt.y, w, h,
                     flags if self.on_progman else flags | SWP_NOZORDER)

    def check(self) -> str:
        """'ok', 'lost' (Explorer restarted and took us with it) or 'gone' (the monitor is gone)."""
        if not self.alive():
            return "lost"
        mons = monitors()
        if self.index >= len(mons):
            return "gone"
        rect = mons[self.index]["rect"]
        if GetAncestor(self.hwnd, GA_PARENT) != self.layer or not IsWindow(self.layer):
            self.rect = rect
            self.attach()
        elif rect != self.rect or _rect(self.hwnd) != rect:       # the resolution changed or something moved it
            self.rect = rect
            self._place()
        return "ok"
