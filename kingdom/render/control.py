"""The logic of the window and the wallpaper, without any toolkit.

Two hosts use it: render/app.py (GTK 4, Linux) and render/sdl_app.py (pygame-ce /
SDL2, Windows). Each one only translates its events (keys with GDK names, clicks,
wheel, drag) into the input methods here, shows the two layers that _render returns
(world and interface, scaled with NEAREST) and provides the hooks below:
timers, clipboard, opening a folder, window size...

CPU/GPU budget:
  * Hard FPS cap (30). Without active weather it drops to idle_fps.
  * The frame is drawn at low resolution (~850x480) and the GPU scales it with
    NEAREST filtering: it looks pixelated and we spend no CPU scaling.
  * With the wallpaper covered ALL timers are removed (0% CPU, 0% GPU).
"""
import json
import sys
import time
from pathlib import Path

from .. import paths
from ..i18n import ui, words
from .iso import POWER_KEYS, TW, Painter, detail_for, fit_scale, zoom_scales

CONTINUE, REMOVE = True, False      # what a timer returns (same as GLib)


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


_STATE: dict = {}


def _read_state(path: Path, mtime: int) -> dict:
    """state.json parsed once per change for the whole process (several monitors share it;
    the painter only reads it)."""
    key = (str(path), mtime)
    if _STATE.get("key") != key:
        _STATE["data"] = json.loads(path.read_text(encoding="utf-8"))
        _STATE["key"] = key
    return _STATE["data"]


class Controller:
    """View state + everything that happens on input, without knowing which toolkit draws."""

    def __init__(self, args, cfg: dict):
        rc = cfg["render"]
        self.args, self.cfg = args, cfg
        self.max_fps = min(30, args.fps or rc["max_fps"])
        self.idle_fps = min(self.max_fps, rc["idle_fps"])
        bc = cfg.get("battery", {})
        self.battery_max_fps = bc.get("max_fps", 15)
        self.battery_idle_fps = bc.get("idle_fps", 3)
        self.away_fps = rc.get("away_fps", 2)
        self.on_battery = False
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
        self._uis = 1.0                # interface scale: never below 1 (readable text in small windows)
        self._last_frame = time.monotonic()
        self._normal_size = (1600, 900)
        self.zoom = 0                  # zoom levels above the base scale (window only)
        self._pointer = (0.0, 0.0)
        self._drag = None
        self._dragged_at = 0.0
        self.paused = False
        self.layered = False           # True: wallpaper (the host decides)
        self.occl = None               # the host's occlusion detector (covered, recompute)
        self.update_check = cfg.get("update", {}).get("check", True)
        self.start_mini = False

    def start(self):
        """Called by the host with the window already created (and self.layered decided)."""
        self.painter.window_mode = not self.layered
        self.interactive = self.interactive or not self.layered   # the window always responds
        self.start_mini = not self.layered and (bool(getattr(self.args, "mini", False)) or _prefs().get("mini", False))

    def started(self):
        """Called by the host after showing the window."""
        self._check_power()
        self.every(30_000, self._check_power)
        self._resume()
        if not self.layered:
            self.call_soon(self._first_run)
            self.every(60_000, self._mark_seen)

    def window_closing(self):
        _save_prefs(last_seen=time.time())

    # -- host hooks ------------------------------------------------------------

    def size(self) -> tuple[int, int]:
        raise NotImplementedError

    def show_frame(self, world, world_scale: float, ui_surf, ui_scale: float, ui_changed: bool):
        """Show the two layers (cairo ARGB32 surfaces) scaled with NEAREST."""
        raise NotImplementedError

    def call_soon(self, fn, *args):
        """Run fn on the interface thread (may be called from another thread)."""
        raise NotImplementedError

    def every(self, ms: int, fn):
        """Timer: fn() returns CONTINUE or REMOVE. Returns an id for cancel()."""
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

    # -- user input (coordinates in view pixels) ---------------------------------

    def key(self, name: str) -> bool:
        """`name` as GDK calls it (Escape, Page_Up, plus, KP_Add, F11...). True if it was used."""
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
        elif name == "e" and not self.layered:          # export the story of the day
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
            return                                  # it was a drag, not a click
        now = time.monotonic()
        p = self.painter
        ux, uy = x / self._uis, y / self._uis
        region = p.ui_region(ux, uy)
        if p.dialog:                                # the dialog awaits an answer: nothing else responds
            action = p.dialog_action_at(ux, uy)
            if action:
                self._dialog_action(action, now)
            self._render()
            return
        if region == "menu_btn":                    # open / close the menu
            p.toggle_menu()
        elif region == "ticker":                    # mini mode: click on a news item -> the camera goes there
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
        elif p.menu_open:                           # click outside: closes the menu
            p.toggle_menu(False)
        elif realm := p.hud_realm_at(ux, uy):       # click on a realm in the list: go see it
            self._focus_realm(realm, now)
        elif entry := p.chron_entry_at(ux, uy):     # click on the chronicle: go where it happened
            self._focus_tile(entry["x"], entry["y"], now)
        elif p.selected_power:                      # with a power selected, the click casts it
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
            # wheel over a panel: scroll it (chronicle: back in time)
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
        self._drag = None                           # without this the mini-mode camera stayed frozen

    def drag_update(self, ox: float, oy: float) -> bool:
        """Offset from the start. True: without zoom, the host must move the window
        (there's no title bar); the drag ends there."""
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
            p.toggle_menu(False)                    # power chosen, the menu closes so you can aim
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
            self.close_window()                        # the daemon (the world) keeps running on its own
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
        """Coming back after a while: what happened meanwhile."""
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
        """Mini mode: small window, no panels, the camera goes by itself to where things happen
        and a strip with the latest news at the bottom. Ideal for a corner of the screen."""
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
        """Spanish <-> English: stored in the config, and the daemon writes in that language
        from now on (the wallpapers follow it through the state)."""
        from .. import config, i18n
        lang = "en" if i18n.LANG == "es" else "es"
        config.set_top_level("language", lang)
        self._send_command({"action": "language", "lang": lang})
        i18n.set_lang(lang)
        self.painter._ui_dirty = True
        self.painter.toast(ui("toast.language"), now, secs=8)

    def _check_update(self, prefs: dict, manual: bool = False):
        """One query to GitHub every 6 hours at most (without blocking the window).
        Asked by hand from the Menu: checks now and, if there's a new version, updates."""
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
        """GIF of how the borders changed today (in another thread: it takes a few seconds)."""
        import threading
        from .. import timelapse
        folder = self._pictures()
        kingdom = (self.painter.state or {}).get("kingdom")
        self.painter.toast(ui("timelapse.working"), time.monotonic(), secs=20)
        self._render()

        def work():
            try:
                path = timelapse.export(None, folder, kingdom)
            except Exception as e:                    # shouldn't happen, but don't let the window die
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
        """PNG card + Markdown story, without IPs or users, in Pictures/Kernel Kingdom.
        The short text goes to the clipboard, ready to paste."""
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
        """Turns the wallpaper on or off (systemd services) without blocking the window."""
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
        """Leaves a request for the daemon (processed on the next tick)."""
        folder = paths.commands_dir()
        folder.mkdir(parents=True, exist_ok=True)
        name = folder / f"{time.time_ns()}.json"
        tmp = name.with_suffix(".tmp")
        tmp.write_text(json.dumps(cmd))
        tmp.rename(name)

    def _send_power(self, power: str, tile: tuple[int, int], now: float):
        """The daemon validates the cost and applies it on the next tick."""
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
        """Travels to a realm's capital (with some zoom) and opens its info card."""
        f = next((f for f in (self.painter.state or {}).get("factions", []) if f["id"] == fid), None)
        if not f:
            return
        if self.zoom < 2 and not self.layered:
            self.zoom = min(2, len(zoom_scales(self._base)) - 1)
        self.painter.focus_tile(*f["capital"])
        self._render()                                  # recomputes the camera with the new zoom
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
        log("paused: wallpaper covered")

    def _resume(self):
        if self.paused:
            log("resumed")
        self.paused = False
        self._poll_state()
        if not self._poll_src:
            self._poll_src = self.every(1000, self._poll_state)
        self._render()
        self._schedule(self._wanted_fps())

    def _wanted_fps(self) -> int:
        top, idle = self.max_fps, self.idle_fps
        if self.on_battery:
            top, idle = min(top, self.battery_max_fps), min(idle, self.battery_idle_fps)
        if ((self.painter.state or {}).get("activity") or {}).get("afk_since"):
            top = idle = min(idle, self.away_fps)               # nobody is watching: barely animate
        if self.painter.wants_fast(time.monotonic()):
            return top
        if self.painter.wants_motion():                      # battles, dragon, fires: smooth but light
            return min(top, max(idle, 15))
        return idle

    def _check_power(self):
        """On battery the frame rate is capped lower (checked every 30 s, it costs nothing)."""
        try:
            import psutil
            battery = psutil.sensors_battery()
        except Exception:                                    # no psutil support, no battery: plugged in
            battery = None
        on = bool(battery and not battery.power_plugged)
        if on != self.on_battery:
            self.on_battery = on
            log("on battery: lower frame rate" if on else "plugged in: normal frame rate")
            if not self.paused:
                self._schedule(self._wanted_fps())
        return CONTINUE

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
            self._frame_src = None      # this source dies when returning REMOVE
            self._schedule(want)
            return REMOVE
        return CONTINUE

    def _poll_state(self):
        """stat() once per second: simpler and sturdier than inotify with renames."""
        if not self.layered:
            self._check_focus_request()
        try:
            mtime = self.state_path.stat().st_mtime_ns
            if mtime != self._mtime:
                self._mtime = mtime
                self.painter.set_state(_read_state(self.state_path, mtime), time.monotonic())
                if self._fps == 0 and not self.paused:   # idle_fps = 0: paint only on changes
                    self._render()
                    self._schedule(self._wanted_fps())
                elif self._fps < self._wanted_fps():
                    self._schedule(self._wanted_fps())
        except (OSError, ValueError):
            pass
        return CONTINUE

    def _check_focus_request(self):
        """Someone clicked a notification: go to that place on the map."""
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
            target = w / (TW * 14)                        # ~14 tiles wide, whatever the size
            self.zoom = min(range(1, len(scales)), key=lambda k: abs(scales[k] - target), default=0)
            self.painter.director_step(now, min(dt, 0.5))
        self.zoom = min(self.zoom, len(scales) - 1)
        scale = scales[self.zoom]
        fw, fh = int(-(-w // scale)), int(-(-h // scale))
        # with zoom, sprites at double resolution: the texture measures fw*u and is scaled scale/u
        self.painter.set_detail(detail_for(scale) if self.zoom else 1)
        world = self.painter.frame(fw, fh, now)
        uis = max(1.0, base)
        self.painter.ui_ratio = scale / uis                # to place the map names in the interface
        ui_surf, changed = self.painter.ui(int(-(-w // uis)), int(-(-h // uis)), now)
        self._scale, self._base, self._uis = scale, base, uis
        self.show_frame(world, scale / self.painter.u, ui_surf, uis, changed)
