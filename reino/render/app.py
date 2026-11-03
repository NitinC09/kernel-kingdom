"""Ventana GTK4 (Linux): superficie layer-shell en la capa BACKGROUND + bucle de frames.

La lógica (entrada, menús, zoom, compartir, ritmo de frames) está en render/control.py;
acá solo va lo de GTK: la ventana, layer-shell, los eventos y las texturas.

Con el fondo tapado (ventana maximizada / pantalla completa / tiling que cubre el
monitor) se eliminan TODOS los timers: el proceso queda dormido en poll() hasta que
el compositor avise que algo cambió. 0% CPU, 0% GPU.

Interacción: clic = ficha de lo que hay (castillo, reino, casilla); el mouse
resalta la casilla. En modo ventana (sin bordes): rueda = zoom hacia el cursor,
arrastrar = mover el mapa (o la ventana, sin zoom), doble clic = sin zoom,
+/- y 0 también; Esc/q cierra, f pantalla completa, h HUD, c crónica.
"""
import os
import signal
from pathlib import Path

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, Gio, GLib, Graphene, Gsk, Gtk  # noqa: E402

try:
    gi.require_version("Gtk4LayerShell", "1.0")
    from gi.repository import Gtk4LayerShell as LayerShell  # noqa: E402
except (ValueError, ImportError):
    LayerShell = None

from .control import Controller, log  # noqa: E402
from .occlusion import OcclusionWatcher  # noqa: E402


class WorldView(Gtk.Widget):
    """Dos texturas chicas escaladas con NEAREST (lo hace la GPU): el mundo (con
    zoom) y la interfaz (siempre a escala base, para que el texto no crezca)."""

    # GTK < 4.10 (Debian 12, antiX...) no tiene append_scaled_texture: ahí escala cairo
    SCALED = hasattr(Gtk.Snapshot, "append_scaled_texture") and not os.environ.get("KK_CAIRO_SCALE")

    def __init__(self):
        super().__init__(hexpand=True, vexpand=True)
        self.layers: list[list] = [[None, 1.0, None], [None, 1.0, None]]   # [textura, escala, superficie]

    def do_snapshot(self, snapshot):
        for tex, scale, surf in self.layers:
            if tex is None:
                continue
            rect = Graphene.Rect().init(0, 0, tex.get_width() * scale, tex.get_height() * scale)
            if self.SCALED:
                snapshot.append_scaled_texture(tex, Gsk.ScalingFilter.NEAREST, rect)
            else:
                cr = snapshot.append_cairo(rect)
                cr.scale(scale, scale)
                cr.set_source_surface(surf, 0, 0)
                cr.get_source().set_filter(cairo.FILTER_NEAREST)
                cr.paint()


def _texture(surf) -> Gdk.Texture:
    # cairo ARGB32 en little-endian == B8G8R8A8 premultiplicado
    return Gdk.MemoryTexture.new(surf.get_width(), surf.get_height(), Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED,
                                 GLib.Bytes.new(bytes(surf.get_data())), surf.get_stride())


def _find_monitor(spec: str) -> Gdk.Monitor | None:
    monitors = list(Gdk.Display.get_default().get_monitors())
    if spec.isdigit():
        return monitors[int(spec)] if int(spec) < len(monitors) else None
    return next((m for m in monitors if m.get_connector() == spec), None)


class Wallpaper(Controller):
    def __init__(self, app: Gtk.Application, args, cfg: dict):
        super().__init__(args, cfg)
        rc = cfg["render"]
        self.win = Gtk.ApplicationWindow(application=app, title="kernel-kingdom")
        self.view = WorldView()
        self.win.connect("realize", self._on_realize)

        self.layered = not args.windowed and LayerShell is not None and LayerShell.is_supported()
        if self.layered:
            self.win.set_child(self.view)
            LayerShell.init_for_window(self.win)
            LayerShell.set_namespace(self.win, "kernel-kingdom")
            LayerShell.set_layer(self.win, LayerShell.Layer.BACKGROUND)
            for edge in (LayerShell.Edge.TOP, LayerShell.Edge.BOTTOM,
                         LayerShell.Edge.LEFT, LayerShell.Edge.RIGHT):
                LayerShell.set_anchor(self.win, edge, True)
            LayerShell.set_exclusive_zone(self.win, -1)
            LayerShell.set_keyboard_mode(self.win, LayerShell.KeyboardMode.NONE)
            if args.monitor is not None:
                monitor = _find_monitor(args.monitor)
                if monitor:
                    LayerShell.set_monitor(self.win, monitor)
                else:
                    log(f"monitor {args.monitor!r} no encontrado, elige el compositor")
        else:
            # Sin bordes ni barra de título: la ventana entera se arrastra para moverla
            self.win.set_decorated(False)
            self.win.set_default_size(1600, 900)
            self.win.set_child(self.view)
            keys = Gtk.EventControllerKey()
            keys.connect("key-pressed", self._on_key)
            self.win.add_controller(keys)
            scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
            scroll.connect("scroll", self._on_scroll)
            self.view.add_controller(scroll)
            drag = Gtk.GestureDrag(button=1)
            drag.connect("drag-begin", lambda g, x, y: self.drag_begin(x, y))
            drag.connect("drag-update", self._on_drag_update)
            drag.connect("drag-end", lambda g, ox, oy: self.drag_end())
            self.view.add_controller(drag)

        self.start()
        if self.interactive:
            click = Gtk.GestureClick(button=1)
            click.connect("released", lambda g, n, x, y: self.click(x, y, n))
            self.view.add_controller(click)
            right = Gtk.GestureClick(button=3)
            right.connect("released", lambda *_: self.right_click())
            self.view.add_controller(right)
            motion = Gtk.EventControllerMotion()
            motion.connect("motion", lambda c, x, y: self.motion(x, y))
            motion.connect("leave", lambda c: self.leave())
            self.view.add_controller(motion)

        if self.layered and not args.no_occlusion:
            try:
                self.occl = OcclusionWatcher(rc["coverage"])
                chan = GLib.IOChannel.unix_new(self.occl.fileno())
                GLib.io_add_watch(chan, GLib.PRIORITY_DEFAULT,
                                  GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR,
                                  self._on_wayland)
            except (OSError, KeyError, ConnectionError) as e:
                log(f"sin detección de oclusión ({e})")
                self.occl = None

        if self.start_mini:
            self.win.set_default_size(480, 300)          # antes de mostrarla: así sí arranca chiquita
        self.win.present()
        if not self.layered:
            self.win.connect("close-request", lambda *_: self.window_closing() and False)
        self.started()

    # -- ganchos de Controller ------------------------------------------------

    def size(self):
        return self.view.get_width(), self.view.get_height()

    def show_frame(self, world, world_scale, ui_surf, ui_scale, ui_changed):
        self.view.layers[0] = [_texture(world), world_scale, world]
        if ui_changed or self.view.layers[1][0] is None or self.view.layers[1][1] != ui_scale:
            self.view.layers[1] = [_texture(ui_surf), ui_scale, ui_surf]
        self.view.queue_draw()

    def call_soon(self, fn, *args):
        GLib.idle_add(fn, *args)

    def every(self, ms, fn):
        if ms % 1000 == 0:
            return GLib.timeout_add_seconds(ms // 1000, fn)
        return GLib.timeout_add(ms, fn)

    def cancel(self, src):
        GLib.source_remove(src)

    def close_window(self):
        self.win.close()

    def quit_app(self):
        self.win.get_application().quit()

    def present_window(self):
        self.win.present()

    def toggle_fullscreen(self):
        self.win.unfullscreen() if self.win.is_fullscreen() else self.win.fullscreen()

    def is_maximized_or_fullscreen(self):
        return self.win.is_maximized() or self.win.is_fullscreen()

    def restore_window(self):
        self.win.unfullscreen()
        self.win.unmaximize()

    def _resize(self, w: int, h: int):
        """En Wayland una ventana ya abierta no se puede achicar desde la app (y en un
        compositor en mosaico el tamaño lo decide él): queda como tamaño para la próxima
        vez que se abra. Abrir en mini sí arranca chiquita."""
        self.win.set_default_size(w, h)

    def set_clipboard(self, text):
        self.win.get_clipboard().set_content(Gdk.ContentProvider.new_for_value(text))   # API de GTK 4.0

    def open_folder(self, folder: Path):
        try:
            Gio.AppInfo.launch_default_for_uri(folder.as_uri(), None)
        except GLib.Error:
            pass

    def pictures_dir(self):
        return GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES) or str(Path.home() / "Pictures")

    # -- eventos de GTK -------------------------------------------------------

    def _on_key(self, ctrl, keyval, keycode, state):
        return self.key(Gdk.keyval_name(keyval) or "")

    def _on_scroll(self, ctrl, dx, dy):
        self.scroll(dy)
        return True

    def _on_drag_update(self, gesture, ox, oy):
        start = self._drag and self._drag["start"]
        if self.drag_update(ox, oy):      # sin zoom: arrastrar mueve la ventana (no hay barra de título)
            sx, sy = start
            self.win.get_surface().begin_move(gesture.get_device(), 1, sx + ox, sy + oy,
                                              gesture.get_current_event_time())
            gesture.reset()

    # -- monitor y oclusión --------------------------------------------------

    def _on_realize(self, win):
        surface = win.get_surface()
        if self.layered and not self.interactive:
            surface.set_input_region(cairo.Region())   # los clics atraviesan el fondo
        surface.connect("enter-monitor", self._on_enter_monitor)

    def _on_enter_monitor(self, surface, monitor: Gdk.Monitor):
        geo = monitor.get_geometry()
        log(f"en monitor {monitor.get_connector()} {geo.width}x{geo.height}")
        if self.occl:
            self.occl.output_name = monitor.get_connector()
            self.occl.output_size = (geo.width, geo.height)
            self.occl.recompute()
            self._apply_occlusion()

    def _on_wayland(self, chan, cond):
        try:
            if self.occl.dispatch():
                self._apply_occlusion()
            return GLib.SOURCE_CONTINUE
        except (ConnectionError, OSError) as e:
            log(f"oclusión desactivada: {e}")
            self.occl = None
            self._resume()
            return GLib.SOURCE_REMOVE


def run(args, cfg: dict):
    # La ventana es una sola: abrirla de nuevo (lanzador o terminal) trae al frente la existente.
    # Los fondos de pantalla son uno por monitor, así que esos sí pueden ser varios.
    if args.windowed:
        app = Gtk.Application(application_id="dev.kernelkingdom.Window", flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
    else:
        app = Gtk.Application(application_id="dev.kernelkingdom.Wallpaper", flags=Gio.ApplicationFlags.NON_UNIQUE)
    holder = {}

    def activate(a):
        if "wp" in holder:
            holder["wp"].win.present()
        else:
            holder["wp"] = Wallpaper(a, args, cfg)
    app.connect("activate", activate)
    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, app.quit)
    app.run([])
