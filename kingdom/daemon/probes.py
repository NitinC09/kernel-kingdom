"""System sampling.

One pass over the process tree per tick reading a single file per process
(/proc/<pid>/stat: name, parent, CPU times, start time and RSS); with hundreds of
processes, psutil opens several per process and costs 10 times more.
Outside Linux psutil is used (on Windows without the ".exe", so the names in the
config work on both systems). The cmdline is only read for truncated names and
for container processes, and it is cached.
"""
import os
import platform
import re
import subprocess
import time

import psutil

from ..compat import WINDOWS
from . import ntproc

_ATTRS = ["pid", "ppid", "name", "memory_info", "cpu_times", "create_time"]
MB = 1024 * 1024
_TICK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
# Linux: kernel version. Windows: the build number (changes with every big update)
KERNEL = platform.version() if WINDOWS else platform.release()
_PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


def scan_proc(long_names: dict) -> dict[int, dict] | None:
    """{pid: {pid, ppid, name, rss, cpu, create_time}} reading /proc/<pid>/stat.
    `long_names` caches names longer than 15 letters (the kernel truncates them).
    None if there is no /proc (psutil is used)."""
    try:
        boot = psutil.boot_time()
        pids = [int(d) for d in os.listdir("/proc") if d.isdigit()]
    except OSError:
        return None
    out = {}
    for pid in pids:
        try:
            with open(f"/proc/{pid}/stat", "rb") as f:
                raw = f.read()
        except OSError:
            continue                                   # it ended between listing and reading
        i, j = raw.find(b"("), raw.rfind(b")")
        name = raw[i + 1:j].decode("utf-8", "replace")
        f = raw[j + 2:].split()
        try:
            start = boot + int(f[19]) / _TICK
            if len(name) == 15:                        # truncated name: the full one comes from the cmdline
                key = (pid, start)
                if key not in long_names:
                    try:
                        with open(f"/proc/{pid}/cmdline", "rb") as c:
                            arg0 = c.read().split(b"\0", 1)[0].decode("utf-8", "replace")
                        full = os.path.basename(arg0.split(" ")[0])
                        long_names[key] = full if full.startswith(name) else name
                    except OSError:
                        long_names[key] = name
                name = long_names[key]
            out[pid] = {"pid": pid, "ppid": int(f[1]), "name": name, "rss": int(f[21]) * _PAGE,
                        "cpu": (int(f[11]) + int(f[12])) / _TICK, "create_time": start}
        except (IndexError, ValueError):
            continue
    return out


# Windows: kernel pseudo-processes. "System Idle Process" (pid 0) accumulates the IDLE time
# of every core: unfiltered, it would be the "culprit" of every storm.
_WIN_PSEUDO = {0, 4}                                       # Idle, System
_WIN_PSEUDO_NAMES = {"System Idle Process", "System", "Registry", "Memory Compression", "Secure System"}


def _loopback(nic: str) -> bool:
    return nic == "lo" or nic.startswith("Loopback")      # Windows: "Loopback Pseudo-Interface 1"


def _load1() -> float:
    try:
        return os.getloadavg()[0]
    except (AttributeError, OSError):                      # Windows: psutil emulates it
        try:
            return psutil.getloadavg()[0]
        except (AttributeError, OSError):
            return 0.0


def _arg_after(cmdline: list[str], flags: tuple[str, ...]) -> str | None:
    for i, arg in enumerate(cmdline[:-1]):
        if arg in flags:
            return cmdline[i + 1]
    return None


class SystemProbe:
    def __init__(self, cfg: dict):
        self.browser_alias = {
            alias: faction
            for faction, aliases in cfg["browsers"].items()
            for alias in aliases
        }
        fort = cfg["fortresses"]
        self.databases = set(fort["databases"])
        self.daemons = set(fort["daemons"])
        self.editors = set(cfg["dev"]["editors"])
        self.compilers = set(cfg["dev"]["compilers"])
        apps = cfg["apps"]
        self.app_ignore = set(apps["ignore"])
        self.app_ignore_prefix = tuple(apps["ignore_prefix"])
        self.app_min_rss = apps["min_rss_mb"]
        self.app_max = apps["max"]
        self._prev_pids: set[int] = set()
        self._prev_io: dict[str, int] | None = None
        self._prev_cpu: dict[int, tuple[float, float]] = {}  # pid -> (create_time, segundos cpu)
        self._last_at = 0.0
        self._last = {"sessions": [], "boots": [], "visits": {}}
        self._prev_t: float | None = None
        self._container_names: dict[tuple[int, float], str | None] = {}
        self._long_names: dict[tuple[int, float], str] = {}
        self._temp_at, self._temp = -1e9, None
        self._services: dict[int, bool] = {}
        self._nt = ntproc.snapshot_function() if WINDOWS else None    # Windows: all processes in one call
        self._nt_checked = False        # Windows: pid -> runs as a service
        psutil.cpu_percent(None)  # initializes the internal counter

    # -- classification -------------------------------------------------------

    def _root_key(self, info: dict) -> tuple[str, str] | None:
        """Is this process the root of a faction or a container?"""
        name = info["name"] or ""
        if name in self.browser_alias:
            return ("browser", self.browser_alias[name])
        if name == "conmon" or name.startswith("containerd-shim"):
            cname = self._container_name(info)
            if cname:
                return ("container", cname)
        return None

    def _container_name(self, info: dict) -> str | None:
        key = (info["pid"], info["create_time"])
        if key not in self._container_names:
            try:
                cmd = psutil.Process(info["pid"]).cmdline()
            except psutil.Error:
                cmd = []
            # Podman: conmon ... -n <nombre>. Docker: containerd-shim ... -id <id>
            name = _arg_after(cmd, ("-n", "--name"))
            if not name:
                cid = _arg_after(cmd, ("-id", "--id"))
                name = f"docker-{cid[:8]}" if cid else None
            self._container_names[key] = name
        return self._container_names[key]

    def _classify(self, procs: dict[int, dict]) -> dict[int, tuple[str, str] | None]:
        """Assigns each pid the key of its nearest ancestor that is a root."""
        memo: dict[int, tuple[str, str] | None] = {}
        for pid in procs:
            path, cur, result = [], pid, None
            while cur in procs and cur not in memo and len(path) < 64:
                path.append(cur)
                result = self._root_key(procs[cur])
                if result:
                    break
                cur = procs[cur]["ppid"]
            else:
                result = memo.get(cur)
            for p in path:
                memo[p] = result
        return memo

    def _scan_nt(self) -> dict[int, dict] | None:
        """Windows fast path. The first time it's checked against what psutil says about this
        very process; if anything disagrees (an unexpected Windows layout), psutil is used from then on."""
        try:
            data = self._nt()
        except OSError:
            data = None
        if data and not self._nt_checked:
            me = data.get(os.getpid())
            try:
                started = psutil.Process().create_time()
            except psutil.Error:
                started = None
            if not (me and me["name"].lower().startswith("python") and me["ppid"] == os.getppid()
                    and started is not None and abs(me["create_time"] - started) < 2):
                data = None
            self._nt_checked = True
        if not data:
            self._nt = None
            return None
        procs = {}
        for pid, info in data.items():
            name = info["name"]
            if pid in _WIN_PSEUDO or name in _WIN_PSEUDO_NAMES or not name:
                continue
            if name[-4:].lower() == ".exe":
                info["name"] = name[:-4]
            procs[pid] = info
        return procs

    def _service(self, pid: int, procs: dict | None = None) -> bool:
        """Windows: does it run as a service (SYSTEM, LOCAL/NETWORK SERVICE)? Checked once per process.
        With the fast sampler the session says it for free: services live in session 0."""
        info = (procs or {}).get(pid)
        if info and "session" in info:
            return info["session"] == 0
        if pid not in self._services:
            try:
                user = (psutil.Process(pid).username() or "").upper()
            except psutil.AccessDenied:                # another user's or protected: the system's
                user = "SYSTEM"
            except psutil.Error:
                user = ""
            self._services[pid] = user.endswith(("SYSTEM", "LOCAL SERVICE", "NETWORK SERVICE"))
        return self._services[pid]

    def _is_app(self, name: str) -> bool:
        return bool(name) and name not in self.app_ignore and not name.startswith(self.app_ignore_prefix)

    @staticmethod
    def _io_counters() -> dict[str, int]:
        net = psutil.net_io_counters(pernic=True)
        disk = psutil.disk_io_counters()
        return {
            "net_rx": sum(v.bytes_recv for k, v in net.items() if not _loopback(k)),
            "net_tx": sum(v.bytes_sent for k, v in net.items() if not _loopback(k)),
            "disk_r": disk.read_bytes if disk else 0,
            "disk_w": disk.write_bytes if disk else 0,
        }

    def _cpu_temp(self) -> float | None:
        """Reading the sensors takes several ms: refreshed every 10 s."""
        now = time.monotonic()
        if now - self._temp_at < 10:
            return self._temp
        self._temp_at, self._temp = now, None
        try:
            temps = psutil.sensors_temperatures()
        except (AttributeError, OSError):
            return None
        for chip in ("k10temp", "coretemp", "zenpower", "cpu_thermal"):
            if temps.get(chip):
                self._temp = max(t.current for t in temps[chip])
                break
        return self._temp

    def logins(self) -> dict:
        """Open sessions, reboots and past visits according to `last` (wtmp/wtmpdb).
        psutil.users() doesn't work with wtmpdb; `last` does. Queried once a minute."""
        now = time.monotonic()
        if now - self._last_at < 60:
            return self._last
        self._last_at = now
        if WINDOWS:                    # no wtmp: visits only come from psutil.users()
            return self._windows_logins()
        try:
            out = subprocess.run(["last", "-n", "400", "-w", "--time-format", "iso"],
                                 capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.SubprocessError):
            return self._last
        sessions, boots, visits = [], [], {}
        iso = re.compile(r"\d{4}-\d\d-\d\dT")
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 4 or parts[0] == "wtmp":
                continue
            if parts[0] == "reboot":
                boots.append({"kernel": parts[3].rstrip("*"), "start": parts[4] if len(parts) > 4 else ""})
                continue
            user, tty = parts[0], parts[1]
            host, rest = ("", parts[2:]) if iso.match(parts[2]) else (parts[2], parts[3:])
            visits[host or "local"] = visits.get(host or "local", 0) + 1
            if "still logged in" in line:
                sessions.append({"user": user, "tty": tty, "host": host, "start": rest[0]})
        self._last = {"sessions": sessions, "boots": boots[:40], "visits": visits}
        return self._last

    def _windows_logins(self) -> dict:
        """Open sessions (console or Remote Desktop) and the current boot."""
        sessions, visits = [], {}
        try:
            users = psutil.users()
        except (AttributeError, OSError):
            users = []
        for u in users:
            host = u.host or ""
            visits[host or "local"] = visits.get(host or "local", 0) + 1
            start = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(u.started)) if u.started else ""
            sessions.append({"user": u.name, "tty": u.terminal or "console", "host": host, "start": start})
        boots = [{"kernel": KERNEL,
                  "start": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(psutil.boot_time()))}]
        self._last = {"sessions": sessions, "boots": boots, "visits": visits}
        return self._last

    # -- muestreo -----------------------------------------------------------

    def sample(self) -> dict:
        now = time.monotonic()
        wall = time.time()
        dt = (now - self._prev_t) if self._prev_t else None
        self._prev_t = now

        procs = scan_proc(self._long_names)
        if procs is None and self._nt:
            procs = self._scan_nt()
        if procs is None:
            procs = {}
            for p in psutil.process_iter(_ATTRS, ad_value=None):
                i = p.info
                name = i["name"] or ""
                if WINDOWS and (i["pid"] in _WIN_PSEUDO or name in _WIN_PSEUDO_NAMES):
                    continue
                if WINDOWS and name[-4:].lower() == ".exe":
                    name = name[:-4]
                procs[i["pid"]] = {"pid": i["pid"], "ppid": i["ppid"], "name": name,
                                   "rss": i["memory_info"].rss if i["memory_info"] else 0,
                                   "cpu": (i["cpu_times"].user + i["cpu_times"].system) if i["cpu_times"] else None,
                                   "create_time": i["create_time"]}
        self._long_names = {k: v for k, v in self._long_names.items() if k[0] in procs}

        # CPU per process = delta of CPU time between ticks (% of one core)
        pcpu: dict[int, float] = {}
        cur_cpu: dict[int, tuple[float, float]] = {}
        for pid, info in procs.items():
            total = info["cpu"]
            if total is None:
                continue
            cur_cpu[pid] = (info["create_time"], total)
            prev = self._prev_cpu.get(pid)
            if dt and prev and prev[0] == info["create_time"]:
                pcpu[pid] = max(0.0, (total - prev[1]) / dt * 100)
        self._prev_cpu = cur_cpu
        self._container_names = {
            k: v for k, v in self._container_names.items() if k[0] in procs
        }

        browsers: dict[str, dict] = {}
        fortresses: dict[str, dict] = {}
        editors: dict[str, int] = {}
        compilers: dict[str, dict] = {}
        apps: dict[str, dict] = {}

        def group(table, key, info, **extra):
            g = table.setdefault(key, {"procs": 0, "rss_mb": 0.0, "cpu": 0.0, "started": wall, **extra})
            g["procs"] += 1
            g["rss_mb"] += info["rss"] / MB
            g["cpu"] += pcpu.get(info["pid"], 0.0)
            if info["create_time"]:
                g["started"] = min(g["started"], info["create_time"])

        owners = self._classify(procs)
        for pid, info in procs.items():
            name = info["name"] or ""
            owner = owners.get(pid)
            if owner and owner[0] == "browser":
                group(browsers, owner[1], info)
            elif owner and owner[0] == "container":
                group(fortresses, f"container:{owner[1]}", info, kind="container", name=owner[1])

            # Services and tools: direct match by name
            if name in self.databases:
                group(fortresses, f"db:{name}", info, kind="db", name=name)
            elif name in self.daemons:
                group(fortresses, f"daemon:{name}", info, kind="daemon", name=name)
            elif name in self.editors:
                parent = procs.get(info["ppid"])
                if not parent or parent["name"] != name:  # count instances, not children
                    editors[name] = editors.get(name, 0) + 1
            elif name in self.compilers:
                group(compilers, name, info)
            elif owner is None and self._is_app(name):
                if WINDOWS:
                    group(apps, name, info, _pid=info["pid"])
                else:
                    group(apps, name, info)

        # 60% of the minimum: the world uses the difference as hysteresis (no founding/abandoning in a loop)
        if WINDOWS:          # Windows services (SYSTEM...) aren't your apps: they don't found hamlets
            self._services = {p: v for p, v in self._services.items() if p in procs}
            apps = {n: g for n, g in apps.items() if not self._service(g.pop("_pid"), procs)}
        apps = dict(sorted(((n, g) for n, g in apps.items() if g["rss_mb"] >= 0.6 * self.app_min_rss),
                           key=lambda kv: -kv[1]["rss_mb"])[:self.app_max + 3])
        for table in (browsers, fortresses, compilers, apps):
            for g in table.values():
                g["since"] = round(g["started"], 1)   # create_time of the oldest process
                g["uptime"] = round(wall - g.pop("started"), 1)
                g["rss_mb"] = round(g["rss_mb"], 1)
                g["cpu"] = round(g["cpu"], 1)

        top = sorted(
            ((pcpu[pid], procs[pid]["name"]) for pid in pcpu if pcpu[pid] > 5),
            reverse=True,
        )[:3]
        pids = set(procs)
        births = len(pids - self._prev_pids) if self._prev_pids else 0
        deaths = len(self._prev_pids - pids) if self._prev_pids else 0
        self._prev_pids = pids
        io = self._io_counters()
        rates = {k: 0.0 for k in io}
        if self._prev_io and dt:
            rates = {k: round(max(0, io[k] - self._prev_io[k]) / dt / MB, 2) for k in io}
        self._prev_io = io

        vm = psutil.virtual_memory()
        return {
            "t": wall,
            "dt": dt or 0.0,
            "cpu": psutil.cpu_percent(None),
            "ram": vm.percent,
            "ram_used_mb": round((vm.total - vm.available) / MB),
            "ram_total_mb": round(vm.total / MB),
            "swap": psutil.swap_memory().percent,
            "load1": round(_load1(), 2),
            "cores": psutil.cpu_count() or 1,
            "procs": len(procs),
            "browsers": browsers,
            "fortresses": fortresses,
            "editors": editors,
            "compilers": compilers,
            "top": [{"name": n, "cpu": round(c, 1)} for c, n in top],
            "apps": apps,
            "births": births,
            "deaths": deaths,
            "cpu_temp": self._cpu_temp(),
            "boot": round(psutil.boot_time()),
            "kernel": KERNEL,
            **self.logins(),               # sessions, boots, visits
            **rates,                       # net_rx, net_tx, disk_r, disk_w in MB/s
        }
