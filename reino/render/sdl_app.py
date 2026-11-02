"""Ventana y fondo de pantalla con pygame-ce (SDL2): el anfitrión de Windows, sin GTK.

La lógica es la misma que en Linux (render/control.py); acá va lo de SDL:
  * Dos texturas en la GPU (Direct3D en Windows) escaladas con NEAREST, como en GTK:
    el mundo (opaco) y la interfaz (alfa premultiplicado, lo que da cairo, con un modo
    de mezcla a medida). Sin acelerador: se compone en CPU con BLEND_PREMULTIPLIED.
  * Un bucle de eventos propio con temporizadores: sin frames pendientes el proceso
    duerme en SDL_WaitEvent (0% CPU), igual que en el main loop de GLib.
  * Fondo de pantalla: la ventana nace oculta y win_desktop la cuelga de la capa del
    escritorio (detrás de los íconos) antes de mostrarla. Pausa con ventanas que lo tapan,
    relanzamiento si Explorer se reinicia.
  * Ventana: normal de Windows (barra de título, ajustar a los bordes, minimizar), una sola
    instancia (abrirla de nuevo trae al frente la que hay).

También corre en Linux/X11 (sin fondo de pantalla): sirve para probarlo sin Windows.
"""
import heapq
import itertools
import os
import queue
import subprocess
import sys
import time
from pathlib import Path

# antes de importar pygame: escalado NEAREST, DPI real en Windows, sin audio ni saludo
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
WAKE = pygame.event.custom_type()            # call_soon desde otro hilo despierta al bucle

# teclas de SDL -> nombres de GDK (los que entiende Controller.key)
_KEYS = {pygame.K_ESCAPE: "Escape", pygame.K_F11: "F11", pygame.K_PAGEUP: "Page_Up",
         pygame.K_PAGEDOWN: "Page_Down", pygame.K_KP_PLUS: "KP_Add", pygame.K_KP_MINUS: "KP_Subtract",
         pygame.K_KP_0: "KP_0"}
_CHARS = {"+": "plus", "-": "minus", "=": "equal"}


def _surface(surf) -> pygame.Surface:
    """Superficie cairo ARGB32 (BGRA en little-endian) -> pygame, sin convertir píxeles."""
    surf.flush()
    return pygame.image.frombuffer(bytes(surf.get_data()), (surf.get_width(), surf.get_height()), "BGRA")


class Loop:
    """Temporizadores y cola de llamadas sobre la cola de eventos de SDL."""

    def __init__(self):
        self._timers: list = []                # heap de (cuándo, n, id)
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
    def __init__(self, args, cfg: dict):
        super().__init__(args, cfg)
        rc = cfg["render"]
        self.loop = Loop()
        self.desktop = None
        self._leaving = False                     # salida a propósito (no relanzar)
        self.layered = not args.windowed
        self._is_fullscreen = False
        if self.layered:
            if not WINDOWS:
                raise SystemExit("kernel-kingdom: el fondo de pantalla con SDL es solo para Windows")
            from .win_desktop import DesktopHost
            self.desktop = DesktopHost(args.monitor)
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
        self._layers = [None, None]                 # [(textura o superficie, escala)] mundo, interfaz
        self._ui_cache = None                        # CPU: interfaz ya escalada
        self._mouse_down = None
        self._last_click = (0.0, 0, 0)

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
                raise pygame.error("sin mezcla premultiplicada")
            return r
        except (pygame.error, ValueError) as e:
            log(f"sin aceleración ({e}): se compone en CPU")
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

    # -- ganchos de Controller ------------------------------------------------

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
                os.startfile(folder)          # noqa: S606  (Explorador de archivos)
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

    # -- Windows: colgarse del escritorio y vigilarlo ---------------------------

    def _attach_desktop(self):
        try:
            self.desktop.attach()
        except OSError as e:
            self._attach_tries += 1
            if self._attach_tries < 50:                # hasta ~5 s
                return CONTINUE
            log(f"no se pudo poner de fondo: {e}")
            self._leaving = True
            self.quit_app()
            return False
        self.win.show()
        self.desktop._place()                      # SDL reubica al mostrar: volver a ponerla en su monitor
        log(f"fondo de pantalla: {self.desktop.describe()}")
        self.every(2000, self._watch_desktop)
        self._render()
        return False

    def _watch_desktop(self):
        try:
            status = self.desktop.check()
        except OSError as e:
            log(f"escritorio: {e}")
            return CONTINUE
        if status == "ok":
            if self.size() != self.desktop.size:     # cambió la resolución
                self._render()
            return CONTINUE
        if status == "lost":
            self.relaunch_desktop()
        else:
            log("el monitor ya no está")
        self._leaving = True
        self.quit_app()
        return False

    def relaunch_desktop(self):
        """Explorer se reinició y se llevó nuestra ventana: otro proceso igual, cuando vuelva."""
        log("Explorer se reinició: relanzando el fondo")
        self._leaving = True
        from .. import cli
        m = self.args.monitor or str(self.desktop.index)
        cli._detach(f"render-{m}", "reino.render", "--monitor", m, "--start-delay", "4")

    def _poll_occlusion(self):
        if self.occl and self.desktop.hwnd and self.occl.poll():
            if self.occl.covered:
                log(f"tapado por {self.occl.culprit}")
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
                self.drag_update(ev.pos[0] - sx, ev.pos[1] - sy)   # sin zoom: la ventana tiene barra propia
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
        while self.loop.running:
            self.loop.run_due()
            if not self.loop.running:
                break
            ev = pygame.event.wait(self.loop.timeout_ms())
            while ev.type != pygame.NOEVENT:
                if ev.type != WAKE:
                    self._on_event(ev)
                ev = pygame.event.poll()


def _already_open() -> bool:
    """Windows: la ventana es una sola. Si ya hay una, se la trae al frente."""
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
    view = SdlView(args, cfg)
    import signal
    signal.signal(signal.SIGINT, lambda *_: view.loop.call_soon(view.quit_app))
    try:
        view.run()
    finally:
        if view.desktop:
            if view.desktop.hwnd and not view._leaving and not view.desktop.alive():
                view.relaunch_desktop()                # Explorer cayó justo antes de salir
            from .win_desktop import refresh_wallpaper
            try:
                refresh_wallpaper()
            except OSError:
                pass
        pygame.quit()
