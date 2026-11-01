"""La lógica de la ventana y del fondo de pantalla, sin ningún toolkit.

Dos anfitriones la usan: render/app.py (GTK 4, Linux) y render/sdl_app.py (pygame-ce /
SDL2, Windows). Cada uno solo traduce sus eventos (teclas con nombres de GDK, clics,
rueda, arrastre) a los métodos de entrada de acá, muestra las dos capas que devuelve
_render (mundo e interfaz, escaladas con NEAREST) y ofrece los ganchos de abajo:
temporizadores, portapapeles, abrir una carpeta, tamaño de la ventana...

Presupuesto de CPU/GPU:
  * Tope duro de FPS (30). Sin clima activo baja a idle_fps.
  * El frame se dibuja a baja resolución (~850x480) y la GPU lo escala con
    filtro NEAREST: se ve pixelado y no gastamos CPU escalando.
  * Con el fondo tapado se eliminan TODOS los timers (0% CPU, 0% GPU).
"""
import json
import sys
import time
from pathlib import Path

from .. import paths
from ..i18n import ui, words
from .iso import POWER_KEYS, TW, Painter, detail_for, fit_scale, zoom_scales

CONTINUE, REMOVE = True, False      # lo que devuelve un temporizador (igual que GLib)


def log(msg: str):
    print(f"kernel-kingdom: {msg}", file=sys.stderr, flush=True)


def _prefs() -> dict:
    try:
        return json.loads((paths.data_dir() / "prefs.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_prefs(**kw):
    prefs = _prefs() | kw
    f = paths.data_dir() / "prefs.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(prefs), encoding="utf-8")


class Controller:
    """Estado de la vista + todo lo que pasa al tocar, sin saber qué toolkit dibuja."""

    def __init__(self, args, cfg: dict):
        rc = cfg["render"]
        self.args, self.cfg = args, cfg
        self.max_fps = min(30, args.fps or rc["max_fps"])
        self.idle_fps = min(self.max_fps, rc["idle_fps"])
        self.pixel_scale = rc["pixel_scale"]
        self.interactive = rc.get("interactive", True)
        self.state_path = Path(args.state)
        self.painter = Painter(cfg["daemon"]["interval"], rc.get("hemisphere", "auto"))
        self.painter.show_chronicle = rc.get("show_chronicle", True)
        self.painter.show_powers = rc.get("show_powers", True)
        self.painter.show_labels = rc.get("labels", False)
        self.painter.hour_override = args.hour
        self.painter.season_override = getattr(args, "season", None)

        self._mtime = None
        self._frame_src = None
        self._poll_src = None
        self._fps = 0
        self._scale = 1.0
        self._base = 1.0
        self._uis = 1.0                # escala de la interfaz: nunca menos de 1 (texto legible en ventanas chicas)
        self._last_frame = time.monotonic()
        self._normal_size = (1600, 900)
        self.zoom = 0                  # niveles de zoom sobre la escala base (solo en ventana)
        self._pointer = (0.0, 0.0)
        self._drag = None
        self._dragged_at = 0.0
        self.paused = False
        self.layered = False           # True: fondo de pantalla (lo decide el anfitrión)
        self.occl = None               # detector de oclusión del anfitrión (covered, recompute)
        self.update_check = cfg.get("update", {}).get("check", True)
        self.start_mini = False

    def start(self):
        """Lo llama el anfitrión con la ventana ya creada (y self.layered decidido)."""
        self.painter.window_mode = not self.layered
        self.interactive = self.interactive or not self.layered   # la ventana siempre responde
        self.start_mini = not self.layered and (bool(getattr(self.args, "mini", False)) or _prefs().get("mini", False))

    def started(self):
        """Lo llama el anfitrión después de mostrar la ventana."""
        self._resume()
        if not self.layered:
            self.call_soon(self._first_run)
            self.every(60_000, self._mark_seen)

    def window_closing(self):
        _save_prefs(last_seen=time.time())

    # -- ganchos del anfitrión -----------------------------------------------

    def size(self) -> tuple[int, int]:
        raise NotImplementedError

    def show_frame(self, world, world_scale: float, ui_surf, ui_scale: float, ui_changed: bool):
        """Mostrar las dos capas (superficies cairo ARGB32) escaladas con NEAREST."""
        raise NotImplementedError

    def call_soon(self, fn, *args):
        """Correr fn en el hilo de la interfaz (se puede llamar desde otro hilo)."""
        raise NotImplementedError

    def every(self, ms: int, fn):
        """Temporizador: fn() devuelve CONTINUE o REMOVE. Devuelve un id para cancel()."""
        raise NotImplementedError

    def cancel(self, src):
        raise NotImplementedError

    def close_window(self):
        raise NotImplementedError

    def quit_app(self):
        raise NotImplementedError

    def present_window(self):
        pass

    def toggle_fullscreen(self):
        pass

    def is_maximized_or_fullscreen(self) -> bool:
        return False

    def restore_window(self):
        pass

    def _resize(self, w: int, h: int):
        pass

    def set_clipboard(self, text: str):
        pass

    def open_folder(self, folder: Path):
        pass

    def pictures_dir(self) -> Path:
        return Path.home() / "Pictures"

    # -- entrada del usuario (coordenadas en píxeles de la vista) --------------

    def key(self, name: str) -> bool:
        """`name` como lo nombra GDK (Escape, Page_Up, plus, KP_Add, F11...). True si se usó."""
        if name == "Escape" and (self.painter.selected_power or self.painter.menu_open):
            self.painter.select_power(None)
            self.painter.toggle_menu(False)
        elif name in ("Escape", "q"):
            self.close_window()
        elif name in "123456789" and len(name) == 1 and self.painter.show_powers:
            key = POWER_KEYS[int(name) - 1]
            self.painter.select_power(None if key == self.painter.selected_power else key)
        elif name in ("f", "F11"):
            self.toggle_fullscreen()
        elif name == "h":
            self.painter.toggle_view("status")
        elif name == "m":
            self._set_mini(not self.painter.mini)
        elif name == "c":
            self.painter.toggle_view("chron")
        elif name == "r":
            self.painter.toggle_view("realms")
        elif name == "e" and not self.layered:          # exportar la historia del día
            self._export_story(time.monotonic())
        elif name in ("Page_Up", "Page_Down"):
            self.painter.scroll_panel("chron", 5 if name == "Page_Up" else -5)
        elif name in ("plus", "equal", "KP_Add"):
            self._zoom_at(1, *self._pointer)
        elif name in ("minus", "KP_Subtract"):
            self._zoom_at(-1, *self._pointer)
        elif name in ("0", "KP_0"):
            self._zoom_reset()
        else:
            return False
        self._render()
        return True

    def click(self, x: float, y: float, n_press: int = 1):
        if self.paused or (self._drag and self._drag["moved"]) or time.monotonic() - self._dragged_at < 0.3:
            return                                  # fue un arrastre, no un clic
        now = time.monotonic()
        p = self.painter
        ux, uy = x / self._uis, y / self._uis
        region = p.ui_region(ux, uy)
        if p.dialog:                                # el diálogo espera respuesta: el resto no responde
            action = p.dialog_action_at(ux, uy)
            if action:
                self._dialog_action(action, now)
            self._render()
            return
        if region == "menu_btn":                    # abrir / cerrar el menú
            p.toggle_menu()
        elif region == "ticker":                    # modo mini: clic en una noticia -> la cámara va ahí
            entry = p.ticker_entry_at(ux, uy)
            if entry:
                p.director_pin(entry, now)
        elif region == "menu":
            self._menu_action(p.menu_action_at(ux, uy), now)
        elif region == "chron_dl":
            self._export_story(now)
        elif region == "chron_share":
            self._share_day(now)
        elif region == "chron_size":
            p.cycle_chronicle()
        elif p.menu_open:                           # clic afuera: cierra el menú
            p.toggle_menu(False)
        elif realm := p.hud_realm_at(ux, uy):       # clic en un reino de la lista: ir a verlo
            self._focus_realm(realm, now)
        elif entry := p.chron_entry_at(ux, uy):     # clic en la crónica: ir a donde pasó
            self._focus_tile(entry["x"], entry["y"], now)
        elif p.selected_power:                      # con un poder elegido, el clic lo lanza
            tile = p.pick(x / self._scale, y / self._scale)
            if tile:
                self._send_power(p.selected_power, tile, now)
            p.select_power(None)
        elif n_press == 2 and not self.layered:
            self._zoom_reset()
            return
        elif region is None:
            p.inspect(x / self._scale, y / self._scale, now, ui_pos=(ux, uy))
        self._render()

    def right_click(self):
        if self.painter.selected_power:
            self.painter.select_power(None)
            self._render()

    def scroll(self, dy: float):
        """dy > 0: rueda hacia abajo."""
        x, y = self._pointer
        panel = self.painter.ui_region(x / self._uis, y / self._uis)
        if panel in ("hud", "chron"):
            # rueda sobre un panel: recorrerlo (crónica: hacia atrás en el tiempo)
            step = 1 if dy > 0 else -1
            self.painter.scroll_panel(panel, step if panel == "hud" else -step)
            self._render()
        else:
            self._zoom_at(-1 if dy > 0 else 1, x, y)

    def motion(self, x: float, y: float):
        self._pointer = (x, y)
        if self.paused:
            return
        tile = self.painter.pick(x / self._scale, y / self._scale)
        if tile != self.painter.hover:
            self.painter.hover = tile
            self._render()

    def leave(self):
        self.painter.hover = None

    def drag_begin(self, x: float, y: float):
        self._drag = {"start": (x, y), "pan": list(self.painter.pan), "moved": False}

    def drag_end(self):
        if self._drag and self._drag["moved"]:
            self._dragged_at = time.monotonic()
        self._drag = None                           # sin esto la cámara del modo mini quedaba congelada

    def drag_update(self, ox: float, oy: float) -> bool:
        """Desplazamiento desde el inicio. True: sin zoom, el anfitrión debe mover la ventana
        (no hay barra de título); el arrastre termina ahí."""
        d = self._drag
        if d is None or (not d["moved"] and abs(ox) + abs(oy) < 5):
            return False
        d["moved"] = True
        if self.zoom == 0:
            self._drag = None
            return True
        self.painter.pan = [d["pan"][0] - ox / self._scale, d["pan"][1] - oy / self._scale]
        self._render()
        return False

    def _menu_action(self, action, now: float):
        p = self.painter
        if not action:
            return
        if action[0] == "toggle":
            p.toggle_view(action[1])
        elif action[0] == "power":
            p.select_power(None if action[1] == p.selected_power else action[1])
            p.toggle_menu(False)                    # elegido el poder, el menú se cierra para apuntar
        elif action[0] == "story":
            self._export_story(now)
        elif action[0] == "share":
            p.toggle_menu(False)
            self._share_day(now)
        elif action[0] == "update":
            p.toggle_menu(False)
            self._do_update()
        elif action[0] == "check_update":
            p.toggle_menu(False)
            self._check_update({}, manual=True)
        elif action[0] == "language":
            self._switch_language(now)
        elif action[0] == "timelapse":
            p.toggle_menu(False)
            self._export_timelapse()
        elif action[0] == "mini":
            p.toggle_menu(False)
            self._set_mini(not p.mini)
        elif action[0] == "wallpaper":
            self._set_wallpaper(not p.wallpaper_on)
        elif action[0] == "quit":
            self.close_window()                        # el daemon (el mundo) sigue corriendo aparte
        elif action[0] == "new_world":
            if p.confirm_new_until > now:           # segundo clic: confirmado
                p.confirm_new_until = 0
                self._send_command({"action": "new_world"})
                p.toast(ui("world.new_sent"), now)
                p.toggle_menu(False)
            else:
                p.confirm_new_until = now + 5
                p._ui_dirty = True

    def _first_run(self):
        from .. import cli
        self.painter.wallpaper_on = cli.wallpaper_status()
        self.painter._ui_dirty = True
        prefs = _prefs()
        if self.start_mini:
            self._set_mini(True)
        from ..compat import WALLPAPER
        if not prefs.get("wallpaper_asked") and WALLPAPER:
            self.painter.show_dialog(ui("dialog.wp.title"),
                                     [(ui("dialog.wp.yes"), "wp_yes"), (ui("dialog.wp.no"), "wp_no")],
                                     ui("dialog.wp.note"))
        else:
            self._show_away(prefs.get("last_seen"))
        _save_prefs(last_seen=time.time())
        if self.update_check:
            self._check_update(prefs)
        self._render()
        return REMOVE

    def _mark_seen(self):
        _save_prefs(last_seen=time.time())
        return CONTINUE

    def _show_away(self, last_seen: float | None):
        """Al volver después de un rato: qué pasó mientras tanto."""
        from .. import story
        if not last_seen or time.time() - last_seen < 20 * 60:
            return
        entries = story.since(last_seen)
        if len(entries) < 3:
            return
        mins = int((time.time() - last_seen) / 60)
        span = f"{mins // 60} h {mins % 60:02d} min" if mins >= 60 else f"{mins} min"
        parts = story.count_parts(entries)
        lines = [ui("dialog.away.counts", events=len(entries), parts=", ".join(parts) or "-")]
        lines += [f"- {t}" for t in story.highlights(entries, 5)]
        self.painter.show_dialog(ui("dialog.away.title", time=span), [(ui("dialog.away.ok"), "close")],
                                 lines=lines)

    def _dialog_action(self, action: str, now: float):
        self.painter.close_dialog()
        if action in ("wp_yes", "wp_no"):
            _save_prefs(wallpaper_asked=True)
            if action == "wp_yes":
                self._set_wallpaper(True)
        elif action == "relaunch":
            from .. import cli
            cli.relaunch_window()
            self.quit_app()

    def _set_mini(self, on: bool):
        """Modo mini: ventana chica, sin paneles, la cámara va sola a donde pasan cosas
        y abajo una tira con lo último. Ideal para tenerlo en un rincón de la pantalla."""
        p = self.painter
        if on == p.mini:
            return
        if on:
            if not self.is_maximized_or_fullscreen():
                w, h = self.size()
                self._normal_size = (w or 1600, h or 900)
            self.restore_window()
            self._resize(480, 300)
            self.zoom = min(2, len(zoom_scales(self._base)) - 1)
            p.director["since"] = -1e9
        else:
            self._resize(*self._normal_size)
            self.zoom = 0
            p.pan = [0.0, 0.0]
        p.mini = on
        p._ui_dirty = True
        _save_prefs(mini=on)
        self._schedule(self._wanted_fps())
        self._render()

    def _switch_language(self, now: float):
        """Castellano <-> inglés: queda en la config y el daemon escribe en ese idioma
        de ahora en más (los fondos de pantalla lo siguen a través del estado)."""
        from .. import config, i18n
        lang = "en" if i18n.LANG == "es" else "es"
        config.set_top_level("language", lang)
        self._send_command({"action": "language", "lang": lang})
        i18n.set_lang(lang)
        self.painter._ui_dirty = True
        self.painter.toast(ui("toast.language"), now, secs=8)

    def _check_update(self, prefs: dict, manual: bool = False):
        """Una consulta a GitHub cada 6 horas como mucho (sin trabar la ventana).
        Pedida a mano desde el Menú: consulta ya y, si hay versión nueva, actualiza."""
        import threading
        from .. import __version__, update

        def show(version):
            now = time.monotonic()
            if version and update._parse(version) > update._parse(__version__):
                self.painter.update_available = version
                self.painter._ui_dirty = True
                if manual:
                    self._do_update()
                    return REMOVE
                self.painter.toast(ui("update.available", version=version), now, secs=8)
            elif manual:
                self.painter.toast(ui("update.latest", version=__version__) if version else ui("update.offline"),
                                   now, ok=bool(version), secs=6)
            self._render()
            return REMOVE

        if manual:
            self.painter.toast(ui("update.checking"), time.monotonic(), secs=10)
            self._render()
        elif time.time() - prefs.get("update_checked", 0) < 6 * 3600:
            show(prefs.get("update_latest"))
            return

        def work():
            version = update.latest()
            if version:
                _save_prefs(update_checked=time.time(), update_latest=version)
            self.call_soon(show, version)
        threading.Thread(target=work, daemon=True).start()

    def _do_update(self):
        import threading
        from .. import cli
        version = self.painter.update_available or "main"
        self.painter.toast(ui("update.working", version=version), time.monotonic(), secs=30)
        self._render()

        def work():
            ok, detail, _ = cli.apply_update()
            self.call_soon(done, ok, detail)

        def done(ok, detail):
            p = self.painter
            if ok:
                p.update_available = None
                _save_prefs(update_checked=0)
                p.show_dialog(ui("dialog.updated.title", version=version),
                              [(ui("dialog.updated.restart"), "relaunch"), (ui("dialog.updated.later"), "close")],
                              ui("dialog.updated.note"))
            else:
                p.toast(ui("update.failed", detail=detail), time.monotonic(), ok=False, secs=10)
            self._render()
            return REMOVE
        threading.Thread(target=work, daemon=True).start()

    def _pictures(self) -> Path:
        folder = Path(self.pictures_dir()) / "Kernel Kingdom"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _export_timelapse(self):
        """GIF de cómo cambiaron las fronteras hoy (en otro hilo: tarda unos segundos)."""
        import threading
        from .. import timelapse
        folder = self._pictures()
        kingdom = (self.painter.state or {}).get("kingdom")
        self.painter.toast(ui("timelapse.working"), time.monotonic(), secs=20)
        self._render()

        def work():
            try:
                path = timelapse.export(None, folder, kingdom)
            except Exception as e:                    # no debería, pero que no muera la ventana
                log(f"timelapse: {e}")
                path = None
            self.call_soon(done, path)

        def done(path):
            now = time.monotonic()
            if path:
                self.painter.toast(ui("timelapse.saved", path=str(path).replace(str(Path.home()), "~")), now, secs=8)
                self.open_folder(folder)
            else:
                self.painter.toast(ui("timelapse.empty"), now, ok=False, secs=8)
            self._render()
            return REMOVE
        threading.Thread(target=work, daemon=True).start()

    def _share_day(self, now: float):
        """Tarjeta PNG + relato en Markdown, sin IPs ni usuarios, en Imágenes/Kernel Kingdom.
        El texto corto queda en el portapapeles, listo para pegar."""
        from .. import story
        from ..i18n import tr
        p = self.painter
        day = time.strftime("%Y-%m-%d")
        entries = story.load(day)
        if not entries:
            p.toast(ui("share.empty"), now, ok=False)
            return
        st = p.state or {}
        kingdom = st.get("kingdom", "")
        date = time.strftime("%d/%m/%Y")
        parts = story.count_parts(entries)
        title = tr("story.title", kingdom=kingdom, date=date)
        subtitle = ui("share.subtitle", date=date, parts=", ".join(parts[:4]) or tr("story.none"))
        lines = [story.scrub(t) for t in story.highlights(entries, 6)]
        folder = self._pictures()
        png, md = folder / f"kernel-kingdom-{day}.png", folder / f"kernel-kingdom-{day}.md"
        p.share_card(title, subtitle, lines, ui("share.footer"), now).write_to_png(str(png))
        md.write_text(story.scrub(story.build(day, entries, kingdom, st.get("era_name", ""))), encoding="utf-8")
        text = "\n".join([title, subtitle, ""] + [f"- {t}" for t in lines] + ["", ui("share.footer")])
        self.set_clipboard(text)
        p.toast(ui("share.saved", path=str(folder).replace(str(Path.home()), "~")), now, secs=8)
        self.open_folder(folder)

    def _set_wallpaper(self, on: bool):
        """Prende o apaga el fondo (servicios de systemd) sin trabar la ventana."""
        import threading
        from .. import cli

        def work():
            msg = ui("toast.wallpaper_on", monitors=", ".join(cli.wallpaper_on())) if on else \
                (cli.wallpaper_off() or ui("toast.wallpaper_off"))
            self.call_soon(done, msg)

        def done(msg):
            self.painter.wallpaper_on = on
            self.painter._ui_dirty = True
            self.painter.toast(msg, time.monotonic(), secs=6)
            self._render()
            return REMOVE
        threading.Thread(target=work, daemon=True).start()

    def _export_story(self, now: float):
        from .. import story
        st = self.painter.state or {}
        out = story.export(None, st.get("kingdom"), st.get("era_name", ""))
        if out:
            self.painter.toast(ui("story.saved", path=str(out[0]).replace(str(Path.home()), "~")), now, secs=6)
        else:
            from ..i18n import tr
            self.painter.toast(tr("story.empty", date=time.strftime("%d/%m/%Y")), now, ok=False)

    def _send_command(self, cmd: dict):
        """Deja un pedido para el daemon (lo procesa en el próximo tick)."""
        folder = paths.commands_dir()
        folder.mkdir(parents=True, exist_ok=True)
        name = folder / f"{time.time_ns()}.json"
        tmp = name.with_suffix(".tmp")
        tmp.write_text(json.dumps(cmd))
        tmp.rename(name)

    def _send_power(self, power: str, tile: tuple[int, int], now: float):
        """El daemon valida el costo y lo aplica en el próximo tick."""
        self._send_command({"power": power, "x": tile[0], "y": tile[1]})
        self.painter.toast(ui("powers.sent", power=words("powers")[power][0]), now)

    def _zoom_at(self, delta: int, x: float, y: float):
        scales = zoom_scales(self._base)
        new = max(0, min(len(scales) - 1, self.zoom + delta))
        if new == self.zoom:
            return
        world_pt = self.painter.to_world(x / self._scale, y / self._scale)
        self.zoom = new
        scale = scales[new]
        w, h = self.size()
        fw, fh = int(-(-w // scale)), int(-(-h // scale))
        if self.zoom == 0:
            self.painter.pan = [0.0, 0.0]
        else:
            self.painter.keep_point(world_pt, x / scale, y / scale, fw, fh)
        self._render()

    def _zoom_reset(self):
        self.zoom = 0
        self.painter.pan = [0.0, 0.0]
        self._render()

    def _focus_tile(self, x: int, y: int, now: float):
        if self.zoom < 2 and not self.layered:
            self.zoom = min(2, len(zoom_scales(self._base)) - 1)
        self.painter.focus_tile(x, y)
        self._render()
        w, h = self.size()
        self.painter.inspect(w / 2 / self._scale, h / 2 / self._scale, now, ui_pos=(w / 2 / self._uis, h / 2 / self._uis))
        self._render()

    def _focus_realm(self, fid: int, now: float):
        """Viaja a la capital de un reino (con algo de zoom) y abre su ficha."""
        f = next((f for f in (self.painter.state or {}).get("factions", []) if f["id"] == fid), None)
        if not f:
            return
        if self.zoom < 2 and not self.layered:
            self.zoom = min(2, len(zoom_scales(self._base)) - 1)
        self.painter.focus_tile(*f["capital"])
        self._render()                                  # recalcula cámara con el zoom nuevo
        w, h = self.size()
        cx, cy = w / 2, h / 2
        self.painter.inspect(cx / self._scale, cy / self._scale, now, ui_pos=(cx / self._uis, cy / self._uis))

    def _apply_occlusion(self):
        if self.occl.covered and not self.paused:
            self._pause()
        elif not self.occl.covered and self.paused:
            self._resume()

    def _pause(self):
        self.paused = True
        for src in (self._frame_src, self._poll_src):
            if src:
                self.cancel(src)
        self._frame_src = self._poll_src = None
        self._fps = 0
        log("pausado: fondo tapado")

    def _resume(self):
        if self.paused:
            log("reanudado")
        self.paused = False
        self._poll_state()
        if not self._poll_src:
            self._poll_src = self.every(1000, self._poll_state)
        self._render()
        self._schedule(self._wanted_fps())

    def _wanted_fps(self) -> int:
        if self.painter.wants_fast(time.monotonic()):
            return self.max_fps
        if self.painter.wants_motion():                      # batallas, dragón, fuegos: fluido pero liviano
            return min(self.max_fps, max(self.idle_fps, 15))
        return self.idle_fps

    def _schedule(self, fps: int):
        if self._frame_src:
            self.cancel(self._frame_src)
            self._frame_src = None
        self._fps = fps
        if fps > 0 and not self.paused:
            self._frame_src = self.every(max(1, round(1000 / fps)), self._on_frame)

    def _on_frame(self):
        self._render()
        want = self._wanted_fps()
        if want != self._fps:
            self._frame_src = None      # este source muere al devolver REMOVE
            self._schedule(want)
            return REMOVE
        return CONTINUE

    def _poll_state(self):
        """stat() una vez por segundo: más simple y robusto que inotify con rename."""
        if not self.layered:
            self._check_focus_request()
        try:
            mtime = self.state_path.stat().st_mtime_ns
            if mtime != self._mtime:
                self._mtime = mtime
                self.painter.set_state(json.loads(self.state_path.read_text(encoding="utf-8")), time.monotonic())
                if self._fps == 0 and not self.paused:   # idle_fps = 0: pintar solo al cambiar
                    self._render()
                    self._schedule(self._wanted_fps())
                elif self._fps < self._wanted_fps():
                    self._schedule(self._wanted_fps())
        except (OSError, ValueError):
            pass
        return CONTINUE

    def _check_focus_request(self):
        """Alguien tocó una notificación: ir a ese lugar del mapa."""
        from ..notify import focus_file
        f = focus_file()
        if not f.exists():
            return
        try:
            req = json.loads(f.read_text())
            f.unlink()
        except (OSError, ValueError):
            return
        if time.time() - req.get("t", 0) < 60 and self.painter.state:
            self.present_window()
            self._focus_tile(req["x"], req["y"], time.monotonic())

    def _render(self):
        w, h = self.size()
        if w <= 0 or h <= 0:
            return
        now = time.monotonic()
        dt, self._last_frame = now - self._last_frame, now
        base = fit_scale(w, h, self.painter.content_size(), self.pixel_scale)
        scales = zoom_scales(base)
        if self.painter.mini and not (self._drag and self._drag["moved"]):
            target = w / (TW * 14)                        # ~14 casillas de ancho, sea cual sea el tamaño
            self.zoom = min(range(1, len(scales)), key=lambda k: abs(scales[k] - target), default=0)
            self.painter.director_step(now, min(dt, 0.5))
        self.zoom = min(self.zoom, len(scales) - 1)
        scale = scales[self.zoom]
        fw, fh = int(-(-w // scale)), int(-(-h // scale))
        # con zoom, sprites al doble de resolución: la textura mide fw*u y se escala scale/u
        self.painter.set_detail(detail_for(scale) if self.zoom else 1)
        world = self.painter.frame(fw, fh, now)
        uis = max(1.0, base)
        self.painter.ui_ratio = scale / uis                # para ubicar los nombres del mapa en la interfaz
        ui_surf, changed = self.painter.ui(int(-(-w // uis)), int(-(-h // uis)), now)
        self._scale, self._base, self._uis = scale, base, uis
        self.show_frame(world, scale / self.painter.u, ui_surf, uis, changed)
