"""Window and wallpaper with pygame-ce (SDL2): the Windows host, no GTK.

The logic is the same as on Linux (render/control.py); the SDL parts are here:
  * Two textures on the GPU (Direct3D on Windows) scaled with NEAREST, as in GTK:
    the world (opaque) and the interface (premultiplied alpha, what cairo produces, with a
    custom blend mode). Without an accelerator: composited on the CPU with BLEND_PREMULTIPLIED.
  * Its own event loop with timers: with no frames pending the process sleeps
    (~0% CPU), like in GLib's main loop.
  * Wallpaper: the window is born hidden and win_desktop hangs it from the desktop
    layer (behind the icons) before showing it. Pauses behind windows that cover it,
    relaunches if Explorer restarts.
  * Window: a normal Windows window (title bar, snapping, minimize), a single
    instance (opening it again brings the existing one forward).

It also runs on Linux/X11 (without the wallpaper): handy for testing without Windows.
"""
import heapq
import itertools
import os
import queue
import subprocess
import sys
import time
from pathlib import Path

# before importing pygame: NEAREST scaling, real DPI on Windows, no audio and no greeting
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
os.environ.setdefault("SDL_RENDER_SCALE_QUALITY", "0")
os.environ.setdefault("SDL_WINDOWS_DPI_AWARENESS", "permonitorv2")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame  # noqa: E402
from pygame._sdl2 import video  # noqa: E402

from .control import CONTINUE, Controller, log  # noqa: E402

WINDOWS = sys.platform == "win32"
WINDOW_TITLE = "Kernel Kingdom"
# SDL_ComposeCustomBlendMode(ONE, ONE_MINUS_SRC_ALPHA, ADD, ONE, ONE_MINUS_SRC_ALPHA, ADD): alfa premultiplicado
PREMULTIPLIED = 1 | 2 << 4 | 6 << 8 | 1 << 16 | 2 << 20 | 6 << 24
WAKE = pygame.event.custom_type()
_RELAUNCH = {"done": False}            # call_soon from another thread wakes up the loop

# SDL keys -> GDK names (the ones Controller.key understands)
_KEYS = {pygame.K_ESCAPE: "Escape", pygame.K_F11: "F11", pygame.K_PAGEUP: "Page_Up",
         pygame.K_PAGEDOWN: "Page_Down", pygame.K_KP_PLUS: "KP_Add", pygame.K_KP_MINUS: "KP_Subtract",
         pygame.K_KP_0: "KP_0"}
_CHARS = {"+": "plus", "-": "minus", "=": "equal"}


def _surface(surf) -> pygame.Surface:
    """cairo ARGB32 surface (BGRA in little-endian) -> pygame, sharing the pixels (no copy): the
    texture upload right after reads them before cairo draws the next frame."""
    surf.flush()
    return pygame.image.frombuffer(surf.get_data(), (surf.get_width(), surf.get_height()), "BGRA")


class Loop:
    """Timers and a call queue on top of SDL's event queue."""

    def __init__(self):
        self._timers: list = []                # heap of (when, n, id)
        self._live: dict[int, tuple] = {}       # id -> (ms, fn)
        self._ids = itertools.count(1)
        self._seq = itertools.count()
        self._soon: queue.SimpleQueue = queue.SimpleQueue()
        self.running = True

    def every(self, ms: int, fn) -> int:
        tid = next(self._ids)
        self._live[tid] = (ms, fn)
        heapq.heappush(self._timers, (time.monotonic() + ms / 1000, next(self._seq), tid))
        return tid

    def cancel(self, tid):
        self._live.pop(tid, None)

    def call_soon(self, fn, *args):
        self._soon.put((fn, args))
        try:
            pygame.event.post(pygame.event.Event(WAKE))
        except pygame.error:
            pass

    def run_due(self):
        while True:
            try:
                fn, args = self._soon.get_nowait()
            except queue.Empty:
                break
            fn(*args)
        now = time.monotonic()
        while self._timers and self._timers[0][0] <= now:
            _, _, tid = heapq.heappop(self._timers)
            entry = self._live.get(tid)
            if not entry:
                continue                           # cancelado
            ms, fn = entry
            if fn() and tid in self._live:
                heapq.heappush(self._timers, (max(now, time.monotonic()) + ms / 1000, next(self._seq), tid))
            else:
                self._live.pop(tid, None)

    def timeout_ms(self) -> int:
        while self._timers and self._timers[0][2] not in self._live:
            heapq.heappop(self._timers)
        if not self._soon.empty():
            return 0
        if not self._timers:
            return 1000
        return max(0, min(1000, int((self._timers[0][0] - time.monotonic()) * 1000) + 1))


class SdlView(Controller):
    def __init__(self, args, cfg: dict, loop: Loop | None = None, monitor: str | None = None):
        super().__init__(args, cfg)
        rc = cfg["render"]
        self.loop = loop or Loop()                # several monitors in one process share the loop
        self.desktop = None
        self._leaving = False                     # deliberate exit (don't relaunch)
        self.layered = not args.windowed
        self._is_fullscreen = False
        if self.layered:
            if not WINDOWS:
                raise SystemExit("kernel-kingdom: the SDL wallpaper is Windows-only")
            from .win_desktop import DesktopHost
            self.desktop = DesktopHost(monitor if monitor is not None else args.monitor)
            left, top = self.desktop.rect[:2]
            self.win = pygame.Window(self.desktop.title, self.desktop.size, (left, top),
                                     borderless=True, hidden=True, utility=True)
        else:
            self.win = pygame.Window(WINDOW_TITLE, (1600, 900), resizable=True, hidden=True)
            self._set_icon()
        self.start()
        if self.start_mini:
            self.win.size = (480, 300)
        self._gpu = self._make_renderer()
        self._layers = [None, None]                 # [(texture or surface, scale)] world, interface
        self._ui_cache = None                        # CPU: interface already scaled
        self._mouse_down = None
        self._last_click = (0.0, 0, 0)
        self._frames = 0

        if self.desktop:
            if not args.no_occlusion:
                self.occl = self.desktop.occlusion(rc["coverage"])
                self.every(1000, self._poll_occlusion)
            self._attach_tries = 0
            self.every(100, self._attach_desktop)
        else:
            self.win.show()
            self.win.focus()
        self.started()

    # -- dibujo ---------------------------------------------------------------

    def _make_renderer(self):
        if os.environ.get("KK_SDL_CPU"):
            return None
        try:
            r = video.Renderer(self.win, accelerated=1)
            probe = video.Texture(r, (1, 1), streaming=True)
            probe.blend_mode = PREMULTIPLIED
            if probe.blend_mode != PREMULTIPLIED:
                raise pygame.error("no premultiplied blending")
            return r
        except (pygame.error, ValueError) as e:
            log(f"no acceleration ({e}): compositing on the CPU")
            return None

    def _upload(self, slot: int, surf, scale: float, blend: int):
        pg = _surface(surf)
        if not self._gpu:
            self._layers[slot] = (pg, scale)
            return
        old = self._layers[slot]
        tex = old[0] if old and old[0].width == pg.get_width() and old[0].height == pg.get_height() else None
        if tex is None:
            tex = video.Texture(self._gpu, pg.get_size(), streaming=True)
            tex.blend_mode = blend
        tex.update(pg)
        self._layers[slot] = (tex, scale)

    def show_frame(self, world, world_scale, ui_surf, ui_scale, ui_changed):
        self._frames += 1
        self._upload(0, world, world_scale, 0)                     # SDL_BLENDMODE_NONE: opaco
        if ui_changed or self._layers[1] is None or self._layers[1][1] != ui_scale:
            self._upload(1, ui_surf, ui_scale, PREMULTIPLIED)
            self._ui_cache = None
        self._present()

    def _present(self):
        if self._gpu:
            r = self._gpu
            r.draw_color = (0, 0, 0, 255)
            r.clear()
            for layer in self._layers:
                if layer:
                    tex, k = layer
                    tex.draw(dstrect=(0, 0, round(tex.width * k), round(tex.height * k)))
            r.present()
            return
        screen = self.win.get_surface()
        w, h = screen.get_size()
        world = self._layers[0]
        if world:
            surf, k = world
            screen.blit(pygame.transform.scale(surf, (round(surf.get_width() * k), round(surf.get_height() * k))),
                        (0, 0))
        if self._layers[1]:
            if self._ui_cache is None or self._ui_cache.get_size() != (w, h):
                surf, k = self._layers[1]
                self._ui_cache = pygame.transform.scale(surf, (round(surf.get_width() * k),
                                                               round(surf.get_height() * k)))
            screen.blit(self._ui_cache, (0, 0), special_flags=pygame.BLEND_PREMULTIPLIED)
        self.win.flip()

    # -- Controller hooks ------------------------------------------------------

    def size(self):
        return self.win.size

    def call_soon(self, fn, *args):
        self.loop.call_soon(fn, *args)

    def every(self, ms, fn):
        return self.loop.every(ms, fn)

    def cancel(self, src):
        self.loop.cancel(src)

    def close_window(self):
        self.window_closing()
        self.loop.running = False

    def quit_app(self):
        self.loop.running = False

    def present_window(self):
        if not self.layered:
            self.win.restore()
            self.win.focus()

    def toggle_fullscreen(self):
        if self._is_fullscreen:
            self.win.set_windowed()
        else:
            self.win.set_fullscreen(desktop=True)
        self._is_fullscreen = not self._is_fullscreen

    def is_maximized_or_fullscreen(self):
        if self._is_fullscreen:
            return True
        if WINDOWS:
            from .win_desktop import IsZoomed, FindWindowW
            return bool(IsZoomed(FindWindowW(None, WINDOW_TITLE)))
        return False

    def restore_window(self):
        self.win.set_windowed()
        self._is_fullscreen = False
        self.win.restore()

    def _resize(self, w: int, h: int):
        self.win.size = (w, h)
        self._render()

    def set_clipboard(self, text):
        try:
            pygame.scrap.put_text(text)
        except (pygame.error, AttributeError):
            pass

    def open_folder(self, folder: Path):
        try:
            if WINDOWS:
                os.startfile(folder)          # noqa: S606  (File Explorer)
            else:
                subprocess.Popen(["xdg-open", str(folder)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass

    def pictures_dir(self):
        from ..compat import pictures_dir
        return pictures_dir()

    def _set_icon(self):
        try:
            from .. import icon, paths
            png = paths.data_dir() / "kernel-kingdom.png"
            if not png.exists():
                icon.make(png)
            self.win.set_icon(pygame.image.load(str(png)))
        except Exception:
            pass

    # -- Windows: hang from the desktop and watch it -------------------------------

    def _attach_desktop(self):
        try:
            self.desktop.attach()
        except OSError as e:
            self._attach_tries += 1
            if self._attach_tries < 50:                # up to ~5 s
                return CONTINUE
            log(f"couldn't become the wallpaper: {e}")
            self._leaving = True
            self.quit_app()
            return False
        self.win.show()
        self.desktop._place()                      # SDL moves the window when showing it: put it back on its monitor
        log(f"wallpaper: {self.desktop.describe()}")
        self.every(2000, self._watch_desktop)
        self._render()
        return False

    def _watch_desktop(self):
        try:
            status = self.desktop.check()
        except OSError as e:
            log(f"desktop: {e}")
            return CONTINUE
        if status == "ok":
            if self.size() != self.desktop.size:     # the resolution changed
                self._render()
            return CONTINUE
        log("Explorer restarted: relaunching the wallpaper" if status == "lost"
            else "the monitor layout changed: relaunching the wallpaper")
        self.relaunch_desktop()
        self.quit_app()
        return False

    def relaunch_desktop(self):
        """A fresh process with the same monitors, once Explorer (or the new layout) is ready."""
        self._leaving = True
        if _RELAUNCH["done"]:                          # one replacement per process, not one per monitor
            return
        _RELAUNCH["done"] = True
        from .. import cli
        m = self.args.monitor or str(self.desktop.index)
        cli._detach(f"render-{m}", "kingdom.render", "--monitor", m, "--start-delay", "4")

    def _poll_occlusion(self):
        if self.occl and self.desktop.hwnd and self.occl.poll():
            if self.occl.covered:
                log(f"covered by {self.occl.culprit}")
            self._apply_occlusion()
        return CONTINUE

    # -- eventos ----------------------------------------------------------------

    def _on_event(self, ev):
        t = ev.type
        if t in (pygame.QUIT, pygame.WINDOWCLOSE):
            if self.layered:
                self.quit_app()
            else:
                self.close_window()
        elif t in (pygame.WINDOWSIZECHANGED, pygame.WINDOWEXPOSED, pygame.WINDOWRESTORED):
            self._ui_cache = None
            if not self.paused:
                self._render()
        elif not self.interactive:
            return
        elif t == pygame.KEYDOWN and not self.layered:
            name = _KEYS.get(ev.key) or _CHARS.get(ev.unicode) or ev.unicode
            if name:
                self.key(name)
        elif t == pygame.MOUSEMOTION:
            self.motion(*ev.pos)
            if self._mouse_down and self._drag:
                sx, sy = self._mouse_down
                self.drag_update(ev.pos[0] - sx, ev.pos[1] - sy)   # no zoom: the window has its own title bar
        elif t == pygame.MOUSEBUTTONDOWN and ev.button == 1:
            self._mouse_down = ev.pos
            if not self.layered:
                self.drag_begin(*ev.pos)
        elif t == pygame.MOUSEBUTTONUP and ev.button == 1:
            self._mouse_down = None
            self.drag_end()
            now = time.monotonic()
            lt, lx, ly = self._last_click
            double = now - lt < 0.4 and abs(ev.pos[0] - lx) + abs(ev.pos[1] - ly) < 6
            self._last_click = (0.0, 0, 0) if double else (now, *ev.pos)
            self.click(ev.pos[0], ev.pos[1], 2 if double else 1)
        elif t == pygame.MOUSEBUTTONUP and ev.button == 3:
            self.right_click()
        elif t == pygame.MOUSEWHEEL and ev.y:
            self.scroll(-ev.y)
        elif t == pygame.WINDOWLEAVE:
            self.leave()

    def run(self):
        _run([self], self.loop)

    def _heartbeat(self):
        """One line per minute in the log: if the wallpaper 'freezes', this shows whether it's still drawing."""
        where = f"monitor {self.desktop.index}: " if self.desktop else ""
        log(f"{where}alive: {self._frames} frames in the last minute, target fps {self._fps}"
            f"{', PAUSED (' + str(self.occl.culprit) + ')' if self.paused and self.occl else ''}")
        self._frames = 0
        return CONTINUE


def _run(views: list, loop: Loop):
    """Events + timers for every view of the process, sleeping until the next thing is due.

    Wallpapers sleep with time.sleep up to the next timer: SDL_WaitEventTimeout didn't wake
    up on time for windows hanging from Explorer's desktop (another process), and wallpapers
    get no input anyway, so there's nothing to wait for but timers. Covered, that's about one
    wake-up per second. The window does use SDL's blocking wait: there it works, and input
    arrives without delay and without polling."""
    for v in views:
        v.every(60_000, v._heartbeat)
    layered = views[0].layered
    while loop.running:
        loop.run_due()
        if not loop.running:
            break
        if layered:
            for ev in pygame.event.get():
                if ev.type == WAKE:
                    continue
                win = getattr(ev, "window", None)
                for v in [v for v in views if win is not None and v.win == win] or views:
                    v._on_event(ev)
            wait = loop.timeout_ms() / 1000
            if wait > 0:
                time.sleep(wait)
            continue
        ev = pygame.event.wait(loop.timeout_ms())
        while ev.type != pygame.NOEVENT:
            if ev.type != WAKE:
                views[0]._on_event(ev)
            ev = pygame.event.poll()


def _monitors(args) -> list[str | None]:
    """Which monitors this process paints: `--monitor all` (one process for every monitor,
    the default on Windows) or a single one."""
    if args.windowed:
        return [None]
    if args.monitor in (None, "all"):
        from .win_desktop import monitors
        return [str(k) for k in range(len(monitors()))] or ["0"]
    return [args.monitor]


def _already_open() -> bool:
    """Windows: there's a single window. If one is already open, it is brought forward."""
    if not WINDOWS:
        return False
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    _already_open.mutex = kernel32.CreateMutexW(None, False, "Local\\KernelKingdomWindow")
    if ctypes.get_last_error() != 183:                 # ERROR_ALREADY_EXISTS
        return False
    from .win_desktop import FindWindowW, user32
    hwnd = FindWindowW(None, WINDOW_TITLE)
    if hwnd:
        user32.ShowWindow(hwnd, 9)                     # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
    return True


def run(args, cfg: dict):
    if args.windowed and _already_open():
        return
    pygame.display.init()
    loop = Loop()
    views = [SdlView(args, cfg, loop, m) for m in _monitors(args)]
    if not args.windowed and args.monitor in (None, "all"):
        from .win_desktop import monitors
        count = len(views)

        def layout():                                  # a monitor added or removed: start over
            if len(monitors()) != count:
                log("the monitor layout changed: relaunching the wallpaper")
                views[0].relaunch_desktop()
                loop.running = False
                return False
            return CONTINUE
        loop.every(3000, layout)
    import signal
    signal.signal(signal.SIGINT, lambda *_: loop.call_soon(views[0].quit_app))
    try:
        _run(views, loop)
    finally:
        if views[0].desktop:
            if any(v.desktop.hwnd and not v._leaving and not v.desktop.alive() for v in views):
                views[0].relaunch_desktop()             # Explorer died right before exiting
            from .win_desktop import refresh_wallpaper
            try:
                refresh_wallpaper()
            except OSError:
                pass
        pygame.quit()
