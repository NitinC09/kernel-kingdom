"""Linux services: one wallpaper service for every monitor, and the move from per-monitor ones."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from kingdom import cli


class ServicesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.units = Path(self.tmp.name)
        self.calls = []
        self.patches = [mock.patch.object(cli, "UNIT_DIR", self.units),
                        mock.patch.object(cli, "_systemctl", side_effect=lambda *a, **k: self.calls.append(a)),
                        mock.patch.object(cli, "WINDOWS", False)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_per_monitor_services_become_one(self):
        (self.units / cli.OLD_RENDER_TEMPLATE).write_text("old")
        old = {"kernel-kingdom-render@DP-1.service", "kernel-kingdom-render@HDMI-A-1.service"}
        with mock.patch.object(cli, "_old_render_units", return_value=old), \
                mock.patch.object(cli.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="")), \
                mock.patch.object(cli, "_unit_active", return_value=True):
            self.assertTrue(cli._write_units())
        self.assertFalse((self.units / cli.OLD_RENDER_TEMPLATE).exists())
        self.assertIn("-m kingdom.render --monitor all", (self.units / cli.RENDER_SERVICE).read_text())
        for unit in old:
            self.assertIn(("disable", "--now", unit), self.calls)
        self.assertIn(("enable", "--now", cli.RENDER_SERVICE), self.calls)      # it was on: stays on

    def test_off_stays_off(self):
        (self.units / cli.OLD_RENDER_TEMPLATE).write_text("old")
        with mock.patch.object(cli, "_old_render_units", return_value=set()), \
                mock.patch.object(cli.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="")):
            cli._write_units()
        self.assertNotIn(("enable", "--now", cli.RENDER_SERVICE), self.calls)

    def test_monitor_list_survives_rewrites(self):
        cli._write_units("DP-1,HDMI-A-1")
        cli._write_units()                                    # e.g. after an update
        self.assertIn("--monitor DP-1,HDMI-A-1", (self.units / cli.RENDER_SERVICE).read_text())

    def test_without_systemd_one_process(self):
        started = []
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", {"XDG_RUNTIME_DIR": d}), \
                mock.patch.object(cli, "_detach", side_effect=lambda *a: started.append(a)):
            cli._start_shared_wallpaper()
        self.assertEqual(started, [("render-all", "kingdom.render", "--monitor", "all")])



class WithoutSystemdTest(unittest.TestCase):
    """OpenRC user services and XDG autostart (Linux without systemd)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.env = mock.patch.dict("os.environ", {"XDG_CONFIG_HOME": f"{d}/cfg", "XDG_RUNTIME_DIR": f"{d}/run",
                                                  "XDG_STATE_HOME": f"{d}/state"})
        self.env.start()
        self.rc = []
        self.patches = [mock.patch.object(cli, "WINDOWS", False), mock.patch.object(cli, "_systemd", return_value=False),
                        mock.patch.object(cli, "ensure_daemon"), mock.patch.object(cli, "monitors", return_value=["DP-1"]),
                        mock.patch.object(cli, "_start_shared_wallpaper"), mock.patch.object(cli, "install_launcher"),
                        mock.patch.object(cli.services.subprocess, "run",
                                          side_effect=lambda a, **k: self.rc.append(a) or SimpleNamespace(returncode=0))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.env.stop()
        self.tmp.cleanup()

    def args(self, **kw):
        return SimpleNamespace(**({"monitors": "all", "no_wallpaper": False} | kw))

    def test_openrc_user_service_and_autostart(self):
        with mock.patch.object(cli.services, "user_services", return_value=True):
            cli.cmd_install(self.args())
        script = cli.services.script_path().read_text()
        self.assertIn("supervisor=supervise-daemon", script)
        self.assertIn("-m kingdom.daemon", script)
        self.assertIn(["rc-update", "--user", "add", "kernel-kingdom", "default"], self.rc)
        self.assertIn(["rc-service", "--user", "kernel-kingdom", "start"], self.rc)
        self.assertRegex(cli.services.autostart_path().read_text(), r"Exec=.* run\n")      # the wallpaper

    def test_no_user_services_autostart_starts_everything(self):
        with mock.patch.object(cli.services, "user_services", return_value=False):
            cli.cmd_install(self.args(no_wallpaper=False))
            self.assertFalse(cli.services.installed())
            self.assertRegex(cli.services.autostart_path().read_text(), r"Exec=.* run\n")
            cli._linux_autostart(False)                  # wallpaper off: the world still starts
        self.assertRegex(cli.services.autostart_path().read_text(), r"Exec=.* daemon\n")

    def test_uninstall_removes_both(self):
        with mock.patch.object(cli.services, "user_services", return_value=True):
            cli.cmd_install(self.args())
        with mock.patch.object(cli, "cmd_stop"):
            cli.cmd_uninstall(SimpleNamespace())
        self.assertFalse(cli.services.installed())
        self.assertFalse(cli.services.autostart_path().exists())
        self.assertIn(["rc-update", "--user", "del", "kernel-kingdom", "default"], self.rc)


if __name__ == "__main__":
    unittest.main()
