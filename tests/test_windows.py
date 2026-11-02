"""Windows paths, simulated from any system (python -m unittest discover -s tests -v)."""
import os
import struct
import tempfile
import time
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock

from kingdom import compat, config, paths
from kingdom.daemon import inputs, probes


class WindowsPaths(unittest.TestCase):
    def test_native_folders_without_xdg(self):
        env = {"APPDATA": r"C:\Users\u\AppData\Roaming", "LOCALAPPDATA": r"C:\Users\u\AppData\Local"}
        with mock.patch.object(paths, "WINDOWS", True), mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(paths.user_config(), Path(env["APPDATA"]) / "kernel-kingdom" / "config.toml")
            self.assertEqual(paths.save_file(), Path(env["LOCALAPPDATA"]) / "kernel-kingdom" / "world.json")
            self.assertEqual(paths.data_dir(), Path(env["LOCALAPPDATA"]) / "kernel-kingdom")
            self.assertEqual(paths.state_file().parent.parent, Path(tempfile.gettempdir()))

    def test_xdg_still_wins(self):
        with mock.patch.object(paths, "WINDOWS", True), \
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/x/cfg", "APPDATA": "/nope"}, clear=True):
            self.assertEqual(paths.user_config(), Path("/x/cfg/kernel-kingdom/config.toml"))

    def test_linux_unchanged(self):
        with mock.patch.object(paths, "WINDOWS", False), mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(paths.save_file(), Path.home() / ".local/state/kernel-kingdom/world.json")
            self.assertEqual(paths.user_config(), Path.home() / ".config/kernel-kingdom/config.toml")


Mem = namedtuple("Mem", "rss")
Cpu = namedtuple("Cpu", "user system")


class FakeProc:
    def __init__(self, pid, ppid, name, mb):
        self.info = {"pid": pid, "ppid": ppid, "name": name, "memory_info": Mem(mb * 1024 * 1024),
                     "cpu_times": Cpu(1.0, 0.5), "create_time": time.time() - 100}


class WindowsProbe(unittest.TestCase):
    def test_exe_names_match_the_config(self):
        fake = [FakeProc(0, 0, "System Idle Process", 0), FakeProc(4, 0, "System", 1), FakeProc(100, 4, "chrome.exe", 300), FakeProc(101, 100, "chrome.exe", 200),
                FakeProc(200, 4, "msedge.exe", 250), FakeProc(300, 4, "Code.exe", 400), FakeProc(400, 4, "svchost.exe", 900),
                FakeProc(500, 4, "VendorSvc.exe", 400), FakeProc(600, 4, "Cubase15.exe", 600)]
        users = {500: "NT AUTHORITY\\SYSTEM", 600: "PC\\nitin"}
        fake_proc = lambda pid: mock.Mock(username=mock.Mock(return_value=users.get(pid, "PC\\nitin")))
        probe = probes.SystemProbe(config.load())
        with mock.patch.object(probes, "WINDOWS", True), mock.patch.object(probes, "scan_proc", return_value=None), \
                mock.patch.object(probes.psutil, "process_iter", return_value=fake), \
                mock.patch.object(probes.os, "getloadavg", side_effect=AttributeError, create=True), \
                mock.patch.object(probes.psutil, "Process", side_effect=fake_proc):
            probe.sample()
            fake[0].info["cpu_times"] = Cpu(5000.0, 0.0)      # the "idle" one adds up everyone's idle time
            snap = probe.sample()
        self.assertEqual(snap["browsers"]["chrome"]["procs"], 2)
        self.assertIn("edge", snap["browsers"])
        self.assertEqual(snap["editors"], {"Code": 1})
        self.assertNotIn("svchost", snap["apps"])                 # a system process, not a hamlet
        self.assertNotIn("System Idle Process", [t["name"] for t in snap["top"]])   # it's not the culprit
        self.assertNotIn("VendorSvc", snap["apps"])               # a service (SYSTEM): founds no hamlet
        self.assertIn("Cubase15", snap["apps"])                   # an app of yours does
        self.assertNotIn("_pid", snap["apps"]["Cubase15"])
        self.assertIsInstance(snap["load1"], float)
        self.assertTrue(snap["boots"])                            # no `last`: the current boot

    def test_loopback_names(self):
        self.assertTrue(probes._loopback("lo"))
        self.assertTrue(probes._loopback("Loopback Pseudo-Interface 1"))
        self.assertFalse(probes._loopback("Ethernet"))


class WindowsFastSampler(unittest.TestCase):
    """The one-call process table (NtQuerySystemInformation), parsed from a buffer laid out like Windows'."""

    @staticmethod
    def table(procs, base=0x7FF000000000):
        import struct
        recs, names = [], []
        for pid, ppid, name, rss, cpu, created, session in procs:
            r = bytearray(256)
            struct.pack_into("<qqq", r, 32, int((created + 11644473600) * 1e7), int(cpu * 1e7), 0)
            struct.pack_into("<QQ", r, 80, pid, ppid)
            struct.pack_into("<I", r, 100, session)
            struct.pack_into("<Q", r, 144, rss)
            recs.append(r); names.append(name.encode("utf-16-le"))
        buf = bytearray()
        offsets = []
        for k, r in enumerate(recs):
            offsets.append(len(buf)); buf += r
        for k, (r, n) in enumerate(zip(recs, names)):
            at = len(buf); buf += n + b"\0\0"
            off = offsets[k]
            struct.pack_into("<HH", buf, off + 56, len(n), len(n) + 2)
            struct.pack_into("<Q", buf, off + 64, base + at)
            struct.pack_into("<I", buf, off, (offsets[k + 1] - off) if k + 1 < len(recs) else 0)
        return bytes(buf), base

    def test_parse(self):
        from kingdom.daemon import ntproc
        buf, base = self.table([(0, 0, "", 0, 9999.0, 0, 0), (4, 0, "System", 1 << 20, 50.0, 1000.0, 0),
                                (812, 4, "chrome.exe", 300 << 20, 12.5, 1_700_000_000.25, 1)])
        out = ntproc.parse(buf, base)
        self.assertEqual(set(out), {0, 4, 812})
        c = out[812]
        self.assertEqual((c["name"], c["ppid"], c["rss"], c["session"]), ("chrome.exe", 4, 300 << 20, 1))
        self.assertAlmostEqual(c["cpu"], 12.5)
        self.assertAlmostEqual(c["create_time"], 1_700_000_000.25, places=3)

    def test_probe_uses_it_and_spots_services(self):
        import psutil
        from kingdom.daemon import ntproc
        me = psutil.Process()
        rows = [(os.getpid(), os.getppid(), "python.exe", 50 << 20, 1.0, me.create_time(), 1),
                (900, 1, "Cubase15.exe", 600 << 20, 3.0, time.time() - 99, 1),
                (901, 1, "VendorSvc.exe", 400 << 20, 3.0, time.time() - 99, 0),
                (0, 0, "", 0, 9e5, 0, 0)]
        buf, base = self.table(rows)
        probe = probes.SystemProbe(config.load())
        probe._nt = lambda: ntproc.parse(buf, base)
        with mock.patch.object(probes, "WINDOWS", True), mock.patch.object(probes, "scan_proc", return_value=None), \
                mock.patch.object(probes.psutil, "process_iter", side_effect=AssertionError("psutil not needed")), \
                mock.patch.object(probes.os, "getloadavg", side_effect=AttributeError, create=True):
            probe.sample()
            snap = probe.sample()
        self.assertIn("Cubase15", snap["apps"])
        self.assertNotIn("VendorSvc", snap["apps"])          # session 0: a service
        self.assertIsNotNone(probe._nt)

    def test_falls_back_when_the_layout_looks_wrong(self):
        probe = probes.SystemProbe(config.load())
        probe._nt = lambda: {os.getpid(): {"pid": os.getpid(), "ppid": 12345, "name": "garbage", "rss": 0,
                                           "cpu": 0.0, "create_time": 0.0, "session": 1}}
        with mock.patch.object(probes, "WINDOWS", True), mock.patch.object(probes, "scan_proc", return_value=None), \
                mock.patch.object(probes.os, "getloadavg", side_effect=AttributeError, create=True):
            snap = probe.sample()
        self.assertIsNone(probe._nt)                         # psutil from now on
        self.assertGreater(snap["procs"], 0)


class WindowsPresence(unittest.TestCase):
    def test_polled_idle_tracks_activity(self):
        idle = {"s": 1.0}
        with mock.patch.object(inputs, "IdleWatch", side_effect=OSError), \
                mock.patch.object(inputs, "idle_seconds", side_effect=lambda: idle["s"]):
            act = inputs.InputActivity()
            self.assertIsInstance(act.idle, inputs.PolledIdle)
            self.assertEqual(act.source, "idle")
            act.wait(0.05, mock.Mock())
            self.assertTrue(act.idle.active)
            idle["s"] = 120.0                                    # left two minutes ago
            out = act.take(2.0)
            self.assertFalse(act.idle.active)
            self.assertGreaterEqual(out["idle_for"], 119)

    def test_not_used_on_linux(self):
        self.assertIsNone(compat.idle_seconds() if not compat.WINDOWS else None)


class FrameRateBudget(unittest.TestCase):
    """Fewer frames on battery and while nobody is watching (same rules for GTK and SDL)."""

    def view(self, state=None):
        import argparse
        from kingdom.render.control import Controller
        args = argparse.Namespace(fps=None, state="/nonexistent", hour=None, season=None, mini=False)
        v = Controller(args, config.load())
        v.painter.state = state
        v.painter.wants_fast = lambda now: False
        v.painter.wants_motion = lambda: False
        return v

    def test_battery_and_away(self):
        v = self.view({"activity": {}})
        self.assertEqual(v._wanted_fps(), 6)
        v.on_battery = True
        self.assertEqual(v._wanted_fps(), 3)
        v.painter.wants_fast = lambda now: True
        self.assertEqual(v._wanted_fps(), 15)                     # storms on battery: 15, not 30
        v.painter.state = {"activity": {"afk_since": 1.0}}
        self.assertEqual(v._wanted_fps(), 2)                      # away: barely animate

    def test_power_check(self):
        v = self.view()
        v._schedule = lambda fps: None
        battery = mock.Mock(power_plugged=False, percent=50)
        with mock.patch("psutil.sensors_battery", return_value=battery):
            v._check_power()
        self.assertTrue(v.on_battery)
        with mock.patch("psutil.sensors_battery", return_value=None):  # desktop: no battery
            v._check_power()
        self.assertFalse(v.on_battery)


class WindowsShortcuts(unittest.TestCase):
    def test_png_wrapped_in_ico(self):
        with tempfile.TemporaryDirectory() as d:
            png = Path(d) / "i.png"
            png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + struct.pack(">II", 256, 256) + b"rest")
            compat.png_to_ico(png, Path(d) / "i.ico")
            raw = (Path(d) / "i.ico").read_bytes()
            reserved, kind, count = struct.unpack("<HHH", raw[:6])
            w, h, _, _, planes, bpp, size, offset = struct.unpack("<BBBBHHII", raw[6:22])
            self.assertEqual((reserved, kind, count, w, h, bpp, offset), (0, 1, 1, 0, 0, 32, 22))
            self.assertEqual(raw[offset:], png.read_bytes())


class WindowsWallpaper(unittest.TestCase):
    def test_coverage_for_pausing(self):
        from kingdom.render.win_desktop import _cov
        mon = (0, 0, 1920, 1080)
        self.assertGreaterEqual(_cov((-8, -8, 1928, 1040), mon), 0.9)     # maximized (without the taskbar)
        self.assertLess(_cov((0, 0, 960, 1080), mon), 0.9)                 # half the screen
        self.assertEqual(_cov((1920, 0, 3840, 1080), mon), 0.0)            # on the other monitor

    def test_cli_wallpaper_flow(self):
        from kingdom import cli
        with tempfile.TemporaryDirectory() as d:
            env = {"XDG_RUNTIME_DIR": d, "XDG_STATE_HOME": d, "APPDATA": d}
            links, started = [], []
            fake_mons = mock.Mock(return_value=[{"rect": (0, 0, 1920, 1080)}, {"rect": (1920, 0, 3840, 1080)}])
            with mock.patch.dict(os.environ, env), mock.patch.object(cli, "WINDOWS", True), \
                    mock.patch("kingdom.render.win_desktop.monitors", fake_mons, create=True), \
                    mock.patch("kingdom.render.win_desktop.refresh_wallpaper", mock.Mock(), create=True), \
                    mock.patch.object(cli, "ensure_daemon"), \
                    mock.patch.object(cli, "_detach", side_effect=lambda n, *a: started.append((n, a))), \
                    mock.patch.object(compat, "make_shortcut", side_effect=lambda lnk, *a, **k: links.append((lnk.name, a))):
                self.assertEqual(cli.monitors(), ["0", "1"])
                self.assertEqual(cli.wallpaper_on(), ["0", "1"])
                self.assertEqual(started, [("render-all", ("kingdom.render", "--monitor", "all"))])   # one process
                self.assertEqual(links[-1][0], cli.WIN_WALLPAPER_LNK)
                self.assertIn("-m kingdom run", links[-1][1])


class SdlFrontEnd(unittest.TestCase):
    """The Windows host (pygame-ce) runs without a screen using SDL's dummy driver."""

    @classmethod
    def setUpClass(cls):
        try:
            import pygame  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("pygame-ce isn't installed")

    def test_window_renders_and_reacts(self):
        import argparse
        import json
        from kingdom import config
        from kingdom.daemon.__main__ import build_state
        from kingdom.daemon.world import World
        from test_world import snap
        cfg = config.load()
        cfg["world"]["seed"] = 7
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {
                "SDL_VIDEODRIVER": "dummy", "XDG_DATA_HOME": d, "XDG_STATE_HOME": d, "XDG_RUNTIME_DIR": d,
                "KK_SDL_CPU": "1"}):
            state = Path(d) / "state.json"
            world = World(cfg)
            state.write_text(json.dumps(build_state(world, snap(), [], 2.0)), encoding="utf-8")
            (Path(d) / "kernel-kingdom").mkdir()
            (Path(d) / "kernel-kingdom" / "prefs.json").write_text('{"wallpaper_asked": true}')
            import pygame
            from kingdom.render import sdl_app
            pygame.display.init()
            try:
                args = argparse.Namespace(windowed=True, mini=False, monitor=None, state=str(state), config=None,
                                          fps=None, no_occlusion=True, hour=12.0, season=None)
                cfg["update"]["check"] = False
                view = sdl_app.SdlView(args, cfg)
                view.every(1500, view.quit_app)
                view.run()
                self.assertIsNotNone(view._layers[0])                 # the world was drawn
                self.assertIsNotNone(view.painter.state)              # and the state was read
                z = view.zoom
                view._on_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_KP_PLUS, unicode="+"))
                self.assertEqual(view.zoom, z + 1)                    # the numeric keypad + key
                view._on_event(pygame.event.Event(pygame.MOUSEWHEEL, x=0, y=-1))
                self.assertEqual(view.zoom, z)                        # rueda hacia abajo: menos zoom
                w, _ = view.size()
                bx = (w - 30) / view._uis * view._uis                 # Menu button, top right
                for t in (pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP):
                    view._on_event(pygame.event.Event(t, button=1, pos=(int(w - 30), 20)))
                self.assertTrue(view.painter.menu_open)
                view._on_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_ESCAPE, unicode="\x1b"))
                self.assertFalse(view.painter.menu_open)
            finally:
                pygame.quit()

    def test_one_process_paints_every_monitor(self):
        """Windows wallpaper: two monitors, one process, one sprite cache, one state parse."""
        import argparse
        import json
        import pygame
        from kingdom.daemon.__main__ import build_state
        from kingdom.daemon.world import World
        from kingdom.render import control, iso, sdl_app
        from test_world import snap
        cfg = config.load()
        cfg["world"]["seed"] = 7
        cfg["update"]["check"] = False

        class FakeHost:                      # the Windows desktop layer, faked
            def __init__(self, monitor):
                self.index = int(monitor)
                self.rect = (1920 * self.index, 0, 1920 * (self.index + 1), 1080)
                self.size = (480, 270)
                self.title = f"kk-test-{monitor}"
                self.hwnd = 1

            def attach(self): pass
            def _place(self): pass
            def describe(self): return f"monitor {self.index}"
            def check(self): return "ok"
            def alive(self): return True
            def occlusion(self, coverage): return mock.Mock(poll=lambda: False, covered=False, culprit=None)

        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {
                "SDL_VIDEODRIVER": "dummy", "XDG_DATA_HOME": d, "XDG_STATE_HOME": d, "XDG_RUNTIME_DIR": d,
                "KK_SDL_CPU": "1"}), mock.patch.object(sdl_app, "WINDOWS", True), \
                mock.patch("kingdom.render.win_desktop.DesktopHost", FakeHost, create=True), \
                mock.patch("kingdom.render.win_desktop.monitors", lambda: [{}, {}], create=True):
            state = Path(d) / "state.json"
            state.write_text(json.dumps(build_state(World(cfg), snap(), [], 2.0)), encoding="utf-8")
            pygame.display.init()
            parses = []
            real_loads = json.loads
            try:
                args = argparse.Namespace(windowed=False, mini=False, monitor="all", state=str(state), config=None,
                                          fps=None, no_occlusion=False, hour=12.0, season=None)
                loop = sdl_app.Loop()
                with mock.patch.object(control.json, "loads", side_effect=lambda t: parses.append(1) or real_loads(t)):
                    views = [sdl_app.SdlView(args, cfg, loop, m) for m in sdl_app._monitors(args)]
                    loop.every(1500, lambda: setattr(loop, "running", False))
                    sdl_app._run(views, loop)
            finally:
                pygame.quit()
        self.assertEqual(len(views), 2)
        self.assertTrue(all(v._frames > 0 for v in views))             # both monitors painted
        self.assertIs(views[0].painter.sprites, views[1].painter.sprites)
        self.assertIs(views[0].painter.sprites, iso.sprites(1))
        self.assertEqual(len(parses), 1)                               # state.json parsed once for both

    def test_loop_timers(self):
        from kingdom.render.sdl_app import Loop
        loop, hits = Loop(), []
        tid = loop.every(0, lambda: hits.append(1) or len(hits) < 3)
        for _ in range(5):
            loop.run_due()
        self.assertEqual(len(hits), 3)                                 # REMOVE stops the timer
        loop.cancel(tid)
        self.assertEqual(loop.timeout_ms(), 1000)


if __name__ == "__main__":
    unittest.main()
