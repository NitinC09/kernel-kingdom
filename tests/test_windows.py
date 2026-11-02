"""Caminos de Windows, simulados desde cualquier sistema (python -m unittest discover -s tests -v)."""
import os
import struct
import tempfile
import time
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock

from reino import compat, config, paths
from reino.daemon import inputs, probes


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
                FakeProc(200, 4, "msedge.exe", 250), FakeProc(300, 4, "Code.exe", 400), FakeProc(400, 4, "svchost.exe", 900)]
        probe = probes.SystemProbe(config.load())
        with mock.patch.object(probes, "WINDOWS", True), mock.patch.object(probes, "scan_proc", return_value=None), \
                mock.patch.object(probes.psutil, "process_iter", return_value=fake), \
                mock.patch.object(probes.os, "getloadavg", side_effect=AttributeError, create=True):
            probe.sample()
            fake[0].info["cpu_times"] = Cpu(5000.0, 0.0)      # el "idle" suma el tiempo ocioso de todo
            snap = probe.sample()
        self.assertEqual(snap["browsers"]["chrome"]["procs"], 2)
        self.assertIn("edge", snap["browsers"])
        self.assertEqual(snap["editors"], {"Code": 1})
        self.assertNotIn("svchost", snap["apps"])                 # proceso del sistema, no una aldea
        self.assertNotIn("System Idle Process", [t["name"] for t in snap["top"]])   # no es el culpable
        self.assertIsInstance(snap["load1"], float)
        self.assertTrue(snap["boots"])                            # sin `last`: el arranque actual

    def test_loopback_names(self):
        self.assertTrue(probes._loopback("lo"))
        self.assertTrue(probes._loopback("Loopback Pseudo-Interface 1"))
        self.assertFalse(probes._loopback("Ethernet"))


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
            idle["s"] = 120.0                                    # se fue hace dos minutos
            out = act.take(2.0)
            self.assertFalse(act.idle.active)
            self.assertGreaterEqual(out["idle_for"], 119)

    def test_not_used_on_linux(self):
        self.assertIsNone(compat.idle_seconds() if not compat.WINDOWS else None)


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
        from reino.render.win_desktop import _cov
        mon = (0, 0, 1920, 1080)
        self.assertGreaterEqual(_cov((-8, -8, 1928, 1040), mon), 0.9)     # maximizada (sin la barra de tareas)
        self.assertLess(_cov((0, 0, 960, 1080), mon), 0.9)                 # media pantalla
        self.assertEqual(_cov((1920, 0, 3840, 1080), mon), 0.0)            # en el otro monitor

    def test_cli_wallpaper_flow(self):
        from reino import cli
        with tempfile.TemporaryDirectory() as d:
            env = {"XDG_RUNTIME_DIR": d, "XDG_STATE_HOME": d, "APPDATA": d}
            links, started = [], []
            fake_mons = mock.Mock(return_value=[{"rect": (0, 0, 1920, 1080)}, {"rect": (1920, 0, 3840, 1080)}])
            with mock.patch.dict(os.environ, env), mock.patch.object(cli, "WINDOWS", True), \
                    mock.patch("reino.render.win_desktop.monitors", fake_mons, create=True), \
                    mock.patch("reino.render.win_desktop.refresh_wallpaper", mock.Mock(), create=True), \
                    mock.patch.object(cli, "ensure_daemon"), \
                    mock.patch.object(cli, "_detach", side_effect=lambda n, *a: started.append((n, a))), \
                    mock.patch.object(compat, "make_shortcut", side_effect=lambda lnk, *a, **k: links.append((lnk.name, a))):
                self.assertEqual(cli.monitors(), ["0", "1"])
                self.assertEqual(cli.wallpaper_on(), ["0", "1"])
                self.assertEqual([n for n, _ in started], ["render-0", "render-1"])
                self.assertEqual(links[-1][0], cli.WIN_WALLPAPER_LNK)
                self.assertIn("-m reino run", links[-1][1])


class SdlFrontEnd(unittest.TestCase):
    """El anfitrión de Windows (pygame-ce) corre sin pantalla con el driver dummy de SDL."""

    @classmethod
    def setUpClass(cls):
        try:
            import pygame  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("pygame-ce no está instalado")

    def test_window_renders_and_reacts(self):
        import argparse
        import json
        from reino import config
        from reino.daemon.__main__ import build_state
        from reino.daemon.world import World
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
            from reino.render import sdl_app
            pygame.display.init()
            try:
                args = argparse.Namespace(windowed=True, mini=False, monitor=None, state=str(state), config=None,
                                          fps=None, no_occlusion=True, hour=12.0, season=None)
                cfg["update"]["check"] = False
                view = sdl_app.SdlView(args, cfg)
                view.every(1500, view.quit_app)
                view.run()
                self.assertIsNotNone(view._layers[0])                 # se dibujó el mundo
                self.assertIsNotNone(view.painter.state)              # y se leyó el estado
                z = view.zoom
                view._on_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_KP_PLUS, unicode="+"))
                self.assertEqual(view.zoom, z + 1)                    # tecla + del teclado numérico
                view._on_event(pygame.event.Event(pygame.MOUSEWHEEL, x=0, y=-1))
                self.assertEqual(view.zoom, z)                        # rueda hacia abajo: menos zoom
                w, _ = view.size()
                bx = (w - 30) / view._uis * view._uis                 # botón Menú, arriba a la derecha
                for t in (pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP):
                    view._on_event(pygame.event.Event(t, button=1, pos=(int(w - 30), 20)))
                self.assertTrue(view.painter.menu_open)
                view._on_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_ESCAPE, unicode="\x1b"))
                self.assertFalse(view.painter.menu_open)
            finally:
                pygame.quit()

    def test_loop_timers(self):
        from reino.render.sdl_app import Loop
        loop, hits = Loop(), []
        tid = loop.every(0, lambda: hits.append(1) or len(hits) < 3)
        for _ in range(5):
            loop.run_due()
        self.assertEqual(len(hits), 3)                                 # REMOVE detiene el temporizador
        loop.cancel(tid)
        self.assertEqual(loop.timeout_ms(), 1000)


if __name__ == "__main__":
    unittest.main()
