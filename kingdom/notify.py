"""Desktop notifications for the big events (wars, sacks, wonders).

The daemon sends them over D-Bus (org.freedesktop.Notifications), at most one every
`min_minutes`, and not while the game window is open (it has its own toasts).
Clicking a notification opens the window centered on where it happened.
"""
import json
import time

from . import paths
from .compat import WINDOWS

KEYS = {"war.declare", "war.start", "capital.sacked", "fall", "wonder.done", "pirates", "walls.breach",
        "exp.siege.breach", "dyn.reborn", "dragon.leave"}


def focus_file():
    return paths.state_file().parent / "focus.json"


class Notifier:
    def __init__(self, cfg: dict):
        nc = cfg.get("notify", {})
        self.enabled = nc.get("enabled", True)
        self.gap = nc.get("min_minutes", 10) * 60
        self.last = 0.0
        self.where: dict[int, tuple[int, int]] = {}
        self.bus = None
        if not self.enabled or WINDOWS:        # desktop D-Bus: Linux only
            return
        try:
            from gi.repository import Gio, GLib
            self.Gio, self.GLib = Gio, GLib
            self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            self.bus.signal_subscribe("org.freedesktop.Notifications", "org.freedesktop.Notifications",
                                      "ActionInvoked", "/org/freedesktop/Notifications", None,
                                      Gio.DBusSignalFlags.NONE, self._on_action)
        except Exception:                      # no gi, no session bus: no notifications
            self.bus = None

    def push(self, entries: list[dict], kingdom: str, window_open: bool):
        if not self.bus or window_open or time.time() - self.last < self.gap:
            return
        big = [e for e in entries if e.get("k") in KEYS]
        if not big:
            return
        e = big[-1]
        V = self.GLib.Variant
        try:
            reply = self.bus.call_sync(
                "org.freedesktop.Notifications", "/org/freedesktop/Notifications", "org.freedesktop.Notifications",
                "Notify", V("(susssasa{sv}i)", ("Kernel Kingdom", 0, "kernel-kingdom", kingdom, e["text"],
                                                ["default", kingdom],
                                                {"desktop-entry": V("s", "dev.kernelkingdom.Window")}, 12000)),
                self.GLib.VariantType("(u)"), self.Gio.DBusCallFlags.NONE, 2000, None)
        except Exception:
            return
        self.last = time.time()
        if "x" in e:
            self.where[reply.unpack()[0]] = (e["x"], e["y"])

    def pump(self):
        """Handles pending clicks (the daemon has no main loop of its own)."""
        if self.bus:
            ctx = self.GLib.MainContext.default()
            while ctx.pending():
                ctx.iteration(False)

    def _on_action(self, conn, sender, path, iface, signal, params):
        nid, action = params.unpack()
        if nid not in self.where and action != "default":
            return
        where = self.where.pop(nid, None)
        if where:
            focus_file().write_text(json.dumps({"x": where[0], "y": where[1], "t": time.time()}))
        from . import cli
        cli._detach("window", "kingdom.render", "--windowed")
