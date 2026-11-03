"""Every process in one system call on Windows (NtQuerySystemInformation).

psutil asks Windows for the whole process table once *per process*, so a pass over
~350 processes costs ~35 ms every tick. One SystemProcessInformation query returns the
same table once: name, parent, CPU times, start time, working set and session for all of
them, in ~1-2 ms. Session 0 is where services live, which also tells services apart
without asking for each process's user.

The record layout below is the 64-bit one; on anything else `snapshot()` returns None and
the caller uses psutil.
"""
import struct
import sys

_SYSTEM_PROCESS_INFORMATION = 5
_STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
_EPOCH = 11644473600          # seconds between 1601-01-01 (FILETIME) and 1970-01-01
# offsets in SYSTEM_PROCESS_INFORMATION (x64)
_NEXT, _CREATE, _USER, _KERNEL, _NAME_LEN, _NAME_PTR = 0, 32, 40, 48, 56, 64
_PID, _PPID, _SESSION, _WORKING_SET = 80, 88, 100, 144
_MIN_RECORD = 152

SUPPORTED = sys.platform == "win32" and sys.maxsize > 2 ** 32


def parse(buf, base: int) -> dict[int, dict]:
    """Records in `buf` (bytes-like) whose name pointers are addresses relative to `base`
    (the buffer's address in memory)."""
    view = memoryview(buf)
    out, off = {}, 0
    while True:
        if off + _MIN_RECORD > len(view):
            break
        nxt = struct.unpack_from("<I", view, off + _NEXT)[0]
        pid, ppid = struct.unpack_from("<QQ", view, off + _PID)
        name_len = struct.unpack_from("<H", view, off + _NAME_LEN)[0]
        name_ptr = struct.unpack_from("<Q", view, off + _NAME_PTR)[0]
        name = ""
        if name_len and name_ptr >= base:
            start = name_ptr - base
            name = bytes(view[start:start + name_len]).decode("utf-16-le", "replace")
        create, user, kernel = struct.unpack_from("<qqq", view, off + _CREATE)
        out[pid] = {"pid": pid, "ppid": ppid, "name": name,
                    "rss": struct.unpack_from("<Q", view, off + _WORKING_SET)[0],
                    "cpu": (user + kernel) / 1e7,
                    "create_time": create / 1e7 - _EPOCH if create else 0.0,
                    "session": struct.unpack_from("<I", view, off + _SESSION)[0]}
        if not nxt:
            break
        off += nxt
    return out


class Snapshot:
    """Keeps one buffer between calls (it only grows), so a tick allocates nothing."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes = ctypes
        self.query = ctypes.WinDLL("ntdll").NtQuerySystemInformation
        self.query.restype = ctypes.c_ulong
        self.query.argtypes = [wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
        self.buf = ctypes.create_string_buffer(512 * 1024)

    def __call__(self) -> dict[int, dict] | None:
        ctypes = self.ctypes
        needed = ctypes.c_ulong(0)
        for _ in range(4):
            status = self.query(_SYSTEM_PROCESS_INFORMATION, self.buf, len(self.buf), ctypes.byref(needed))
            if status == _STATUS_INFO_LENGTH_MISMATCH:
                self.buf = ctypes.create_string_buffer(max(needed.value, len(self.buf)) + 64 * 1024)
                continue
            if status:
                return None
            return parse(self.buf, ctypes.addressof(self.buf))
        return None


def snapshot_function():
    """The fast sampler, or None where it doesn't apply."""
    if not SUPPORTED:
        return None
    try:
        return Snapshot()
    except (OSError, AttributeError):
        return None
