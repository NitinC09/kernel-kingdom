"""`kernel-kingdom`: a single command for everything.

    kernel-kingdom                 # daemon + wallpaper on every monitor, in the background
    kernel-kingdom window          # opens the window (zoom, powers) and gives the terminal back
    kernel-kingdom window --mini   # small window with an automatic camera, for a corner of the screen
    kernel-kingdom stop            # stops everything
    kernel-kingdom install         # launcher in the menu + autostart with the session
    kernel-kingdom uninstall
    kernel-kingdom doctor          # checks dependencies and compositor support
    kernel-kingdom config          # creates ~/.config/kernel-kingdom/config.toml to edit
    kernel-kingdom chronicle       # prints the chronicle
    kernel-kingdom story           # the story of the day as a narrative (.md and .txt)
    kernel-kingdom update          # downloads the latest version and restarts whatever is running
    kernel-kingdom timelapse       # GIF of how the borders changed today
    kernel-kingdom snapshot out.png

On Windows there is no systemd: the wallpaper goes behind the desktop icons (render/win_desktop.py),
`install` puts the launcher in the Start menu and the world and the wallpaper in the Startup folder.
"""
import argparse
import re
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import compat, config, i18n, paths, services
from .compat import WINDOWS
from .i18n import ui

UNIT_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"
DAEMON_UNIT = """[Unit]
Description=kernel-kingdom: system monitor and kingdom simulation

[Service]
ExecStart={python} -m kingdom.daemon
Restart=on-failure
RestartSec=5
Nice=10
CPUQuota=10%
MemoryMax=250M

[Install]
WantedBy=default.target
"""
RENDER_UNIT = """[Unit]
Description=kernel-kingdom: wallpaper on every monitor
PartOf=graphical-session.target
After=graphical-session.target kernel-kingdom-daemon.service
Wants=kernel-kingdom-daemon.service

[Service]
ExecStart={python} -m kingdom.render --monitor {monitors}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=graphical-session.target
"""


def monitors() -> list[str]:
    """Connectors of the monitors (DP-1, HDMI-A-1...) asking the compositor.
    On Windows: indices ("0" the primary, "1"...)."""
    if WINDOWS:
        from .render.win_desktop import monitors as win_monitors
        return [str(k) for k in range(len(win_monitors()))]
    from .wl import Connection
    names: dict[int, str] = {}
    conn = None

    def on_global(name, iface, ver):
        if iface == "wl_output" and ver >= 4:
            oid = conn.bind(name, iface, 4)
            names[oid] = ""
            conn.adopt(oid, lambda op, r, oid=oid: names.__setitem__(oid, r.s()) if op == 4 else None)
    try:
        conn = Connection(on_global)
        conn.roundtrip()
        conn.roundtrip()
    except (OSError, KeyError, ConnectionError, TimeoutError):
        return []
    return [n for n in names.values() if n]


APP_ID = "dev.kernelkingdom.Window"
DEV_ROOT = Path(__file__).resolve().parent.parent
IS_CHECKOUT = (DEV_ROOT / "pyproject.toml").exists() and (DEV_ROOT / "kingdom").is_dir()
DATA = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def _logs() -> Path:
    d = paths.save_file().parent / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pidfile(name: str) -> Path:
    d = paths.state_file().parent / "pids"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{name}.pid"


def _alive(pid: int) -> bool:
    return compat.pid_alive(pid)          # on Windows os.kill(pid, 0) would kill the process


def _detach(name: str, module: str, *args: str) -> int:
    """Starts a process detached from the terminal (no output on screen: it goes to a log)."""
    log = open(_logs() / f"{name}.log", "a", encoding="utf-8")
    env = dict(os.environ)
    if IS_CHECKOUT:
        env["PYTHONPATH"] = f"{DEV_ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}".rstrip(os.pathsep)
    proc = subprocess.Popen([sys.executable, "-m", module, *args], stdin=subprocess.DEVNULL, stdout=log,
                            stderr=log, cwd=DEV_ROOT if IS_CHECKOUT else None, env=env, **compat.detach_kwargs())
    _pidfile(name).write_text(str(proc.pid))
    return proc.pid


def _running(name: str) -> bool:
    f = _pidfile(name)
    return f.exists() and f.read_text().strip().isdigit() and _alive(int(f.read_text()))


WIN_WORLD_LNK, WIN_WALLPAPER_LNK = "Kernel Kingdom (world).lnk", "Kernel Kingdom (wallpaper).lnk"


def _win_refresh_wallpaper():
    """After closing the wallpapers, let Windows repaint its image (no stale frame is left)."""
    try:
        from .render.win_desktop import refresh_wallpaper
        refresh_wallpaper()
    except OSError:
        pass


def _systemd() -> bool:
    """systemd is the running init (sd_booted: /run/systemd/system) and systemctl exists. Checking
    only for systemctl isn't enough: some OpenRC systems ship it without systemd running."""
    return not WINDOWS and Path("/run/systemd/system").is_dir() and shutil.which("systemctl") is not None


def _unit_active(unit: str) -> bool:
    return shutil.which("systemctl") is not None and subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", unit]).returncode == 0


def _unit_installed(unit: str) -> bool:
    return (UNIT_DIR / unit).exists()


def ensure_daemon():
    """The world runs apart from any window or terminal: a systemd or OpenRC user service if
    installed; otherwise a loose background process."""
    st = paths.state_file()
    if st.exists() and time.time() - st.stat().st_mtime < 10:
        return
    if _systemd() and _unit_installed("kernel-kingdom-daemon.service"):
        _systemctl("start", "kernel-kingdom-daemon.service", check=False)
    elif not WINDOWS and services.installed() and services.start():
        time.sleep(1.5)
    elif not _running("daemon"):
        _detach("daemon", "kingdom.daemon")
        time.sleep(1.5)                       # so the first state exists


def cmd_run(args):
    """Wallpaper on every monitor, in the background. The terminal stays free."""
    if args.foreground:
        return _foreground(args)
    if not compat.WALLPAPER:
        print(ui("cli.no_wallpaper"))
        args.mini = False
        return cmd_window(args)
    ensure_daemon()
    targets = args.monitors.split(",") if args.monitors and args.monitors != "all" else (monitors() or [None])
    if not WINDOWS and _systemd() and (_unit_installed(RENDER_SERVICE) or _unit_installed(OLD_RENDER_TEMPLATE)):
        if _write_units():                                # older per-monitor services: one now
            _systemctl("daemon-reload", check=False)
        _systemctl("start", RENDER_SERVICE, check=False)
    else:
        _start_shared_wallpaper(args.monitors or "all")
    print(ui("cli.started", monitors=", ".join(t or "?" for t in targets)))


def _start_shared_wallpaper(spec: str = "all"):
    """ONE process paints every monitor (each extra process would load Python, the toolkit,
    cairo and the sprites again: ~60-160 MB per monitor). Per-monitor processes from older
    versions are stopped first."""
    for f in (paths.state_file().parent / "pids").glob("render-*.pid"):
        if f.stem != "render-all":
            _stop_pid(f.stem)
    if not _running("render-all"):
        _detach("render-all", "kingdom.render", "--monitor", spec)


def cmd_window(args):
    """Opens the window and gives the terminal back. Closing the window doesn't stop the world."""
    ensure_daemon()
    if args.foreground:
        compat.exec_python("-m", "kingdom.render", "--windowed")
    _detach("window", "kingdom.render", "--windowed", *(["--mini"] if getattr(args, "mini", False) else []))
    print(ui("cli.window"))


def relaunch_window(delay: float = 1.5):
    """Opens a new window once the current one has closed (it's single-instance)."""
    env = dict(os.environ)
    if IS_CHECKOUT:
        env["PYTHONPATH"] = f"{DEV_ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}".rstrip(os.pathsep)
    log = open(_logs() / "window.log", "a", encoding="utf-8")
    boot = (f"import runpy, sys, time; time.sleep({delay}); sys.argv = ['kingdom.render', '--windowed']; "
            "runpy.run_module('kingdom.render', run_name='__main__', alter_sys=True)")
    subprocess.Popen([sys.executable, "-c", boot], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                     cwd=DEV_ROOT if IS_CHECKOUT else None, env=env, **compat.detach_kwargs())


def cmd_update(args):
    from . import __version__, update
    print(ui("cli.update.checking", version=__version__))
    new = update.newer()
    if not new and not args.force:
        print(ui("cli.update.none") if update.latest() else ui("cli.update.offline"))
        return 0
    print(ui("cli.update.installing", version=new or "main"))
    ok, detail, restarted = apply_update()
    if not ok:
        print(ui("cli.update.failed", detail=detail))
        return 1
    print(ui("cli.update.done", version=new or "main", restarted=len(restarted)))
    return 0


def apply_update() -> tuple[bool, str, list[str]]:
    """Installs the latest version and restarts the services (the window uses it too)."""
    from . import update
    ok, detail = update.install()
    if not ok:
        return False, detail, []
    if _unit_installed("kernel-kingdom-daemon.service"):
        _write_units()                            # in case something changed in the services
        _systemctl("daemon-reload", check=False)
        install_launcher()
    restarted = update.restart_services() if _systemd() else []
    if not WINDOWS and services.installed():          # OpenRC: same service, new code
        services.install(sys.executable, str(DEV_ROOT) if IS_CHECKOUT else None, _logs() / "daemon.log")
        services.restart()
        restarted.append(services.SERVICE)
    elif not restarted and _running("daemon"):        # no service manager: the loose world
        _stop_pid("daemon")
        _detach("daemon", "kingdom.daemon")
        restarted.append("daemon")
    if not _systemd() and _running("render-all"):     # the loose wallpaper too
        _stop_pid("render-all")
        _start_shared_wallpaper()
        restarted.append("wallpaper")
    return True, "", restarted


def _stop_pid(name: str):
    """Stops a process started with _detach. On Windows there's no SIGTERM to catch: the daemon
    is asked to exit (so it saves the world) and only terminated if it doesn't respond."""
    f = _pidfile(name)
    pid = int(f.read_text()) if f.exists() and f.read_text().strip().isdigit() else 0
    if pid and _alive(pid):
        if WINDOWS and name == "daemon":
            folder = paths.commands_dir()
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{time.time_ns()}.json").write_text('{"action": "quit"}')
            for _ in range(50):
                if not _alive(pid):
                    break
                time.sleep(0.1)
        if _alive(pid):
            compat.terminate(pid)
    f.unlink(missing_ok=True)


def cmd_stop(args):
    renders = [p.stem for p in (paths.state_file().parent / "pids").glob("render-*.pid")]
    for name in ("window", *renders, "daemon"):
        _stop_pid(name)
    if WINDOWS and renders:
        _win_refresh_wallpaper()
    if not WINDOWS:
        services.stop()
    if _systemd():
        out = subprocess.run(["systemctl", "--user", "list-units", "--plain", "--no-legend", "kernel-kingdom*"],
                             capture_output=True, text=True).stdout
        for line in out.splitlines():
            _systemctl("stop", line.split()[0], check=False)
    print(ui("cli.stopped"))


def _foreground(args):
    """Development mode: everything in this terminal, with logs; Ctrl+C stops it."""
    procs = [subprocess.Popen([sys.executable, "-m", "kingdom.daemon"])]
    time.sleep(1.5)
    procs.append(subprocess.Popen([sys.executable, "-m", "kingdom.render", "--monitor", args.monitors or "all"]))

    def stop(*_):
        for p in procs:
            p.terminate()
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        while all(p.poll() is None for p in procs):
            time.sleep(1)
    finally:
        stop()
        for p in procs:
            p.wait()


def _systemctl(*a, check=True):
    return subprocess.run(["systemctl", "--user", *a], check=check)


def install_launcher():
    """'Kernel Kingdom' launcher in the applications menu (opens the window)."""
    from . import icon
    if WINDOWS:
        png = paths.data_dir() / "kernel-kingdom.png"
        icon.make(png)
        compat.png_to_ico(png, png.with_suffix(".ico"))
        compat.make_shortcut(compat.start_menu() / "Kernel Kingdom.lnk", compat.gui_python(), "-m kingdom window",
                             icon=str(png.with_suffix(".ico")), workdir=str(DEV_ROOT) if IS_CHECKOUT else "",
                             desc="Your computer as a pixel-art kingdom")
        return
    icon.make(DATA / "icons" / "hicolor" / "256x256" / "apps" / "kernel-kingdom.png")
    apps = DATA / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    # Without depending on the working directory (COSMIC's launcher ignores Path=): in a repo clone
    # its run.sh is used, which finds its own place; installed with pip, the module is in the environment.
    exec_line = f"{DEV_ROOT / 'run.sh'} window" if IS_CHECKOUT else f"{sys.executable} -m kingdom window"
    (apps / f"{APP_ID}.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Kernel Kingdom\n"
        "Comment=Your computer as a pixel-art kingdom\nComment[es]=Tu computadora como un reino\n"
        f"Exec={exec_line}\nIcon=kernel-kingdom\nTerminal=false\n"
        f"Categories=Game;Simulation;\nStartupWMClass={APP_ID}\nKeywords=wallpaper;kingdom;\nKeywords[es]=fondo;reino;\n")
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", str(apps)], check=False, capture_output=True)


def cmd_install(args):
    install_launcher()
    if WINDOWS:                                           # autostart: Startup folder
        compat.make_shortcut(compat.startup_folder() / WIN_WORLD_LNK, compat.gui_python(),
                             "-m kingdom daemon", workdir=str(DEV_ROOT) if IS_CHECKOUT else "",
                             desc="Kernel Kingdom: the world simulation")
        ensure_daemon()
        targets = [] if args.no_wallpaper else wallpaper_on()
        print(ui("cli.installed", monitors=", ".join(targets)) if targets else ui("cli.installed_world"))
        print(ui("cli.autostart"))
        print(ui("cli.win_logs", path=_logs()))
        return
    if not _systemd():
        return _install_without_systemd(args)
    _write_units()
    cmd_stop(args)                                        # whatever was running loose goes to systemd
    _systemctl("daemon-reload")
    _systemctl("enable", "--now", "kernel-kingdom-daemon.service")
    targets = [] if args.no_wallpaper else (args.monitors.split(",") if args.monitors != "all" else monitors())
    if targets:
        _write_units(args.monitors)
        _systemctl("daemon-reload")
        _systemctl("enable", "--now", RENDER_SERVICE)
    print(ui("cli.installed", monitors=", ".join(targets)) if targets else ui("cli.installed_world"))
    print(ui("cli.launcher"))
    print(ui("cli.logs"))


RENDER_SERVICE, OLD_RENDER_TEMPLATE = "kernel-kingdom-render.service", "kernel-kingdom-render@.service"


def _old_render_units() -> set[str]:
    """Per-monitor services (kernel-kingdom-render@DP-1.service...) from before 0.4."""
    units = set()
    for cmd in ("list-units", "list-unit-files"):
        out = subprocess.run(["systemctl", "--user", cmd, "--plain", "--no-legend", "--all", "kernel-kingdom-render@*"],
                             capture_output=True, text=True).stdout
        units |= {line.split()[0] for line in out.splitlines() if line.split() and "@." not in line.split()[0]}
    return units


def _session_command(sub: str) -> str:
    """Command line for the desktop's autostart (a clone runs its run.sh, like the launcher)."""
    return f"{DEV_ROOT / 'run.sh'} {sub}" if IS_CHECKOUT else f"{sys.executable} -m kingdom {sub}"


def _linux_autostart(wallpaper: bool):
    """XDG autostart: the wallpaper with the session; or, when no OpenRC user service runs the
    world, at least the world."""
    if wallpaper:
        services.write_autostart(_session_command("run"))
    elif not services.installed():
        services.write_autostart(_session_command("daemon"))
    else:
        services.remove_autostart()


def _install_without_systemd(args):
    """OpenRC 0.60+: the world as an OpenRC user service; the wallpaper (and the world, with
    no user services) from the desktop's XDG autostart."""
    if services.user_services():
        _stop_pid("daemon")                               # a loose world goes to OpenRC
        services.install(sys.executable, str(DEV_ROOT) if IS_CHECKOUT else None, _logs() / "daemon.log")
        print(ui("cli.openrc"))
    elif services.openrc_init():
        print(ui("cli.openrc_old"))
    ensure_daemon()
    targets = [] if args.no_wallpaper else (args.monitors.split(",") if args.monitors != "all" else monitors())
    _linux_autostart(bool(targets))
    if targets:
        _start_shared_wallpaper(args.monitors)
    print(ui("cli.installed", monitors=", ".join(targets)) if targets else ui("cli.installed_world"))
    print(ui("cli.session_autostart", path=services.autostart_path()))
    print(ui("cli.launcher"))
    print(ui("cli.win_logs", path=_logs()))


def _write_units(monitors_spec: str = "all") -> bool:
    """Writes the services. Older per-monitor wallpaper services are replaced by the single one
    (enabled if they were). Returns True if it migrated something (systemd needs a reload)."""
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    workdir = f"WorkingDirectory={DEV_ROOT}\nEnvironment=PYTHONPATH={DEV_ROOT}\n" if IS_CHECKOUT else ""
    (UNIT_DIR / "kernel-kingdom-daemon.service").write_text(
        DAEMON_UNIT.format(python=sys.executable).replace("[Service]\n", "[Service]\n" + workdir))
    existing = UNIT_DIR / RENDER_SERVICE
    if monitors_spec == "all" and existing.exists():                 # keep a monitor list set at install
        m = re.search(r"--monitor (\S+)", existing.read_text())
        monitors_spec = m.group(1) if m else "all"
    existing.write_text(RENDER_UNIT.replace("{monitors}", monitors_spec).format(python=sys.executable)
                        .replace("[Service]\n", "[Service]\n" + workdir))
    old_template = UNIT_DIR / OLD_RENDER_TEMPLATE
    if not old_template.exists():
        return False
    old = _old_render_units()
    was_on = any(subprocess.run(["systemctl", "--user", "is-enabled", "--quiet", u]).returncode == 0
                 or _unit_active(u) for u in old)
    for unit in old:
        _systemctl("disable", "--now", unit, check=False)
    old_template.unlink(missing_ok=True)
    _systemctl("daemon-reload", check=False)
    if was_on:
        _systemctl("enable", "--now", RENDER_SERVICE, check=False)
    return True


# -- wallpaper: on / off (from the terminal or from the app's menu) ------------------

def wallpaper_status() -> bool:
    if not compat.WALLPAPER:
        return False
    if shutil.which("systemctl"):
        out = subprocess.run(["systemctl", "--user", "list-units", "--plain", "--no-legend", "--state=active",
                              "kernel-kingdom-render*"], capture_output=True, text=True).stdout
        if out.strip():
            return True
    return any(_running(p.stem) for p in (paths.state_file().parent / "pids").glob("render-*.pid"))


def wallpaper_on() -> list[str]:
    """Wallpaper on every monitor; with systemd it also starts with the session."""
    ensure_daemon()
    targets = monitors()
    if _systemd():
        _write_units()
        _systemctl("daemon-reload", check=False)
        _systemctl("enable", "--now", RENDER_SERVICE, check=False)
    else:
        _start_shared_wallpaper()
        if not WINDOWS:                                   # so it comes back by itself at sign-in
            _linux_autostart(True)
    if WINDOWS:                                           # so it comes back by itself at sign-in
        compat.make_shortcut(compat.startup_folder() / WIN_WALLPAPER_LNK, compat.gui_python(), "-m kingdom run",
                             workdir=str(DEV_ROOT) if IS_CHECKOUT else "",
                             desc="Kernel Kingdom: the wallpaper")
    return targets


def wallpaper_off():
    if _systemd():
        units = _old_render_units() | {RENDER_SERVICE}
        for unit in units:
            _systemctl("disable", "--now", unit, check=False)
    pids = list((paths.state_file().parent / "pids").glob("render-*.pid"))
    for f in pids:
        _stop_pid(f.stem)
    if not WINDOWS and not _systemd() and services.autostart_path().exists():
        _linux_autostart(False)
    if WINDOWS:
        (compat.startup_folder() / WIN_WALLPAPER_LNK).unlink(missing_ok=True)
        if pids:
            _win_refresh_wallpaper()


def cmd_wallpaper(args):
    if args.state != "status" and not compat.WALLPAPER:
        print(ui("cli.no_wallpaper"))
        return 1
    if args.state == "on":
        print(ui("toast.wallpaper_on", monitors=", ".join(wallpaper_on())))
    elif args.state == "off":
        wallpaper_off()
        print(ui("toast.wallpaper_off"))
    else:
        print("on" if wallpaper_status() else "off")


def cmd_uninstall(args):
    if WINDOWS:
        cmd_stop(args)
        for lnk in (compat.start_menu() / "Kernel Kingdom.lnk", compat.startup_folder() / WIN_WORLD_LNK,
                    compat.startup_folder() / WIN_WALLPAPER_LNK):
            lnk.unlink(missing_ok=True)
        for ext in (".png", ".ico"):
            (paths.data_dir() / f"kernel-kingdom{ext}").unlink(missing_ok=True)
        print(ui("cli.uninstalled", path=paths.save_file()))
        return
    if not _systemd():
        cmd_stop(args)
        services.uninstall()
        services.remove_autostart()
    units = [p.name for p in UNIT_DIR.glob("kernel-kingdom*")] if _systemd() else []
    for name in units:
        if name.endswith("@.service"):
            continue
        _systemctl("disable", "--now", name, check=False)
    if _systemd():
        out = subprocess.run(["systemctl", "--user", "list-units", "--plain", "--no-legend",
                              "kernel-kingdom-render@*"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            _systemctl("disable", "--now", line.split()[0], check=False)
        for name in units:
            (UNIT_DIR / name).unlink(missing_ok=True)
        _systemctl("daemon-reload", check=False)
    (DATA / "applications" / f"{APP_ID}.desktop").unlink(missing_ok=True)
    (DATA / "icons" / "hicolor" / "256x256" / "apps" / "kernel-kingdom.png").unlink(missing_ok=True)
    print(ui("cli.uninstalled", path=paths.save_file()))


def cmd_doctor(args):
    ok = True

    def check(label, cond, hint=""):
        nonlocal ok
        ok &= bool(cond)
        print(f"  {'✔' if cond else '✘'} {label}" + ("" if cond or not hint else f"\n      → {hint}"))

    print(ui("cli.doctor"))
    check(f"Python {sys.version.split()[0]} (>= 3.11)", sys.version_info >= (3, 11))
    try:
        import psutil  # noqa: F401
        check("psutil", True)
    except ImportError:
        check("psutil", False, "pip install psutil")
    if WINDOWS:                                    # no GTK: pygame-ce (SDL2) + pycairo
        try:
            import cairo  # noqa: F401
            import pygame  # noqa: F401
            check("pycairo + pygame-ce", True)
        except ImportError:
            check("pycairo + pygame-ce", False, _deps_hint())
    else:
        try:
            import cairo  # noqa: F401
            import gi
            gi.require_version("Gtk", "4.0")
            check("PyGObject + GTK 4 + pycairo", True)
        except (ImportError, ValueError):
            check("PyGObject + GTK 4 + pycairo", False, _deps_hint())
    if WINDOWS:
        from .compat import idle_seconds
        from .render import win_desktop
        try:
            layer, mons = win_desktop.desktop_layer(), win_desktop.monitors()
        except OSError:
            layer, mons = None, []
        check(ui("cli.win_layer"), layer, ui("cli.win_layer_hint"))
        print(f"    {ui('cli.monitors')}: " + ", ".join(
            f"{k} ({m['rect'][2] - m['rect'][0]}x{m['rect'][3] - m['rect'][1]})" for k, m in enumerate(mons)))
        idle = idle_seconds() is not None
        print(f"  {'✔' if idle else '·'} {ui('cli.win_presence')}: {ui('cli.yes') if idle else ui('cli.no')}")
        print(ui("cli.ok") if ok else ui("cli.missing"))
        return 0 if ok else 1
    try:
        import gi
        gi.require_version("Gtk4LayerShell", "1.0")
        check("gtk4-layer-shell", True)
    except (ImportError, ValueError):
        check("gtk4-layer-shell", False, _deps_hint())
    wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
    check(ui("cli.wayland"), wayland, ui("cli.wayland_hint"))
    if wayland:
        from .wl import Connection
        found = set()
        try:
            c = Connection(lambda n, i, v: found.add(i))
            c.roundtrip()
        except (OSError, ConnectionError, TimeoutError):
            pass
        check(ui("cli.layer"), "zwlr_layer_shell_v1" in found, ui("cli.layer_hint"))
        check(ui("cli.pause"), "zcosmic_toplevel_info_v1" in found, ui("cli.pause_hint"))
        check(ui("cli.idle"), "ext_idle_notifier_v1" in found)
        print(f"    {ui('cli.monitors')}: {', '.join(monitors()) or '?'}")
    in_input = os.access("/dev/input/event0", os.R_OK)
    print(f"  {'✔' if in_input else '·'} {ui('cli.keys')}: {ui('cli.yes') if in_input else ui('cli.no')}")
    print(ui("cli.ok") if ok else ui("cli.missing"))
    return 0 if ok else 1


def _deps_hint() -> str:
    if WINDOWS:
        return f'"{sys.executable}" -m pip install pycairo pygame-ce psutil   (or run install.ps1 again)'
    osr = Path("/etc/os-release").read_text() if Path("/etc/os-release").exists() else ""
    if "ostree" in osr or Path("/run/ostree-booted").exists():
        return "rpm-ostree install python3-gobject gtk4 gtk4-layer-shell  (and reboot)"
    if "fedora" in osr.lower():
        return "sudo dnf install python3-gobject gtk4 gtk4-layer-shell"
    if "alpine" in osr.lower():
        return "doas apk add py3-gobject3 py3-cairo gtk4.0 gtk4-layer-shell"
    if "gentoo" in osr.lower():
        return "sudo env USE=introspection emerge dev-python/pygobject gui-libs/gtk gui-libs/gtk4-layer-shell"
    if "arch" in osr.lower():
        return "sudo pacman -S python-gobject gtk4 gtk4-layer-shell"
    if "debian" in osr.lower() or "ubuntu" in osr.lower():
        return "sudo apt install python3-gi gir1.2-gtk-4.0 libgtk4-layer-shell0 gir1.2-gtk4layershell-1.0"
    return "install PyGObject, GTK 4 and gtk4-layer-shell with your distro's package manager"


def cmd_story(args):
    from . import story
    out = story.export(args.date)
    if not out:
        print(i18n.tr("story.empty", date=args.date or time.strftime("%Y-%m-%d")))
        return 1
    md, txt = out
    if args.print:
        print(md.read_text(encoding="utf-8"))
    print(f"{md}\n{txt}")
    return 0


def cmd_config(args):
    dest = paths.user_config()
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(paths.DEFAULT_CONFIG, dest)
    print(dest)


def main():
    ap = argparse.ArgumentParser(prog="kernel-kingdom", description="Live wallpaper / idle game fed by your system.")
    sub = ap.add_subparsers(dest="cmd")
    run = sub.add_parser("run", help="daemon + wallpaper in the background (default)")
    run.add_argument("--monitors", default="all", help="all, or DP-1,HDMI-A-1")
    run.add_argument("-f", "--foreground", action="store_true", help="in this terminal, with logs (development)")
    win = sub.add_parser("window", help="opens the window (with zoom and powers) and gives the terminal back")
    win.add_argument("-f", "--foreground", action="store_true")
    win.add_argument("--mini", action="store_true", help="small window with an automatic camera (key m)")
    sub.add_parser("stop", help="stops everything (window, wallpaper and daemon)")
    sub.add_parser("daemon", help="only the world, in the background (used by autostart)")
    inst = sub.add_parser("install", help="launcher in the menu + autostart (systemd --user, OpenRC --user or "
                                          "the desktop's autostart; Windows: Startup folder)")
    inst.add_argument("--monitors", default="all")
    inst.add_argument("--no-wallpaper", action="store_true", help="only the world and the launcher, no wallpaper")
    wp = sub.add_parser("wallpaper", help="turn the wallpaper on/off (on, off, status)")
    wp.add_argument("state", choices=["on", "off", "status"])
    tl = sub.add_parser("timelapse", help="GIF of how the borders changed during the day")
    tl.add_argument("--date", help="YYYY-MM-DD (default: today)")
    up = sub.add_parser("update", help="downloads the latest version and restarts whatever is running")
    up.add_argument("--force", action="store_true", help="reinstall even if there's no new version")
    sub.add_parser("uninstall")
    sub.add_parser("doctor")
    sub.add_parser("config", help="creates/shows your user config")
    sub.add_parser("chronicle", help="prints the chronicle")
    st = sub.add_parser("story", help="the story of the day as a narrative (.md and .txt to read or listen to)")
    st.add_argument("--date", help="YYYY-MM-DD (default: today)")
    st.add_argument("--print", action="store_true", help="show it instead of only saving it")
    snap = sub.add_parser("snapshot", help="one frame to PNG")
    snap.add_argument("png")
    snap.add_argument("extra", nargs=argparse.REMAINDER)
    args = ap.parse_args()
    try:
        i18n.set_lang(config.load().get("language", "auto"))
    except (OSError, ValueError):
        i18n.set_lang("auto")

    if args.cmd in (None, "run"):
        if args.cmd is None:
            args.monitors, args.foreground = "all", False
        return cmd_run(args)
    if args.cmd == "chronicle":
        compat.exec_python("-m", "kingdom.daemon", "--chronicle")
    if args.cmd == "snapshot":
        compat.exec_python("-m", "kingdom.render", "--snapshot", args.png, *args.extra)
    if args.cmd == "story":
        return cmd_story(args)
    if args.cmd == "timelapse":
        from . import timelapse
        out = timelapse.export(args.date)
        print(ui("timelapse.saved", path=out) if out else ui("timelapse.empty"))
        return 0 if out else 1
    if args.cmd == "daemon":
        return ensure_daemon()
    return {"window": cmd_window, "stop": cmd_stop, "update": cmd_update, "wallpaper": cmd_wallpaper, "install": cmd_install, "uninstall": cmd_uninstall,
            "doctor": cmd_doctor, "config": cmd_config}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
