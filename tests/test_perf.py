"""Resource budget: if a change makes the world or the drawing much more expensive,
this fails. The limits are generous (slow machines, CI); typical values on
a current PC are ~10x lower."""
import copy
import os
import time
import unittest

from kingdom import config
from kingdom.daemon.probes import SystemProbe, scan_proc
from kingdom.daemon.world import World

from test_world import fortress, proc, snap

SLOW = float(os.environ.get("KK_PERF_SLACK", "1"))   # multiplier for very slow machines


class PerfTest(unittest.TestCase):
    def test_world_tick_is_cheap(self):
        cfg = config.load()
        cfg["world"]["seed"] = 3
        w = World(cfg)
        s = snap(browsers={"chrome": proc(5000, procs=40), "zen": proc(3000, 20), "firefox": proc(2000, 12)},
                 apps={"claude": proc(400, 1), "steam": proc(600, 3)},
                 fortresses={"daemon:sshd": fortress("daemon", "sshd", 5000),
                             "container:web": fortress("container", "web", 9000)}, editors={"nvim": 2})
        for _ in range(300):                       # a grown world
            w.step(copy.deepcopy(s))
        t0 = time.perf_counter()
        for _ in range(100):
            w.step(copy.deepcopy(s))
        per_tick = (time.perf_counter() - t0) / 100
        self.assertLess(per_tick, 0.040 * SLOW, f"tick {per_tick * 1000:.1f} ms")

    @unittest.skipUnless(os.path.isdir("/proc/self"), "Linux")
    def test_system_sample_is_cheap(self):
        p = SystemProbe(config.load())
        p.sample()
        t0 = time.perf_counter()
        for _ in range(5):
            p.sample()
        per = (time.perf_counter() - t0) / 5
        n = len(scan_proc({}) or {})
        # ~0.02 ms per process on a current PC; with margin
        self.assertLess(per, (0.03 + n * 0.0003) * SLOW, f"muestreo {per * 1000:.1f} ms with {n} procesos")

    def test_frame_is_cheap(self):
        try:
            from kingdom.render.iso import Painter
        except ImportError as e:                    # without cairo
            self.skipTest(str(e))
        cfg = config.load()
        cfg["world"]["seed"] = 3
        w = World(cfg)
        s = snap(browsers={"chrome": proc(5000, procs=40), "zen": proc(3000, 20)})
        for _ in range(200):
            w.step(copy.deepcopy(s))
        state = w.to_state()
        state.update(t=time.time(), interval=2.0, events=[], metrics={})
        p = Painter()
        p.set_state(state, 0.0)
        p.frame(640, 360, 0.0)                     # the first one builds the static world
        t0 = time.perf_counter()
        for k in range(30):
            p.frame(640, 360, 0.05 * k)
        per = (time.perf_counter() - t0) / 30
        self.assertLess(per, 0.025 * SLOW, f"frame {per * 1000:.1f} ms")


if __name__ == "__main__":
    unittest.main()
