"""Process enumeration on macOS without ``ps``.

``/bin/ps`` is setuid root, and a Seatbelt sandbox (sandbox-exec, Agent Safehouse)
refuses to exec setuid binaries whatever the profile allows: measured 2026-09-22
under ``safehouse --enable=process-control``, where ``ps``, ``top`` and ``su`` all
fail with EPERM at exec while ``pgrep`` runs and ``proc_listpids`` answers. Without
a snapshot the marker scan is inconclusive, the profile lease is kept "until this
process exits", and every later call meets ``BrowserBusyError``.

So this module answers the same questions ``ps`` did, through libproc and
``sysctl(KERN_PROCARGS2)``, which ``process-info-pidinfo`` permits:

- :func:`process_rows`: pid -> (ppid, pgid, start identity, run-state letter),
  the shape ``process_tree._ps_process_rows`` returns.
- :func:`argv`: a process's argument vector. The sandbox strips the environment
  block from the ``KERN_PROCARGS2`` answer (only argv comes back), so a marker
  carried in the environment is invisible here; the browser launch therefore also
  carries it as a ``--linkedin-mcp-marker=`` switch and :func:`pids_with_argv`
  looks for that.
"""

from __future__ import annotations

import ctypes
import sys

_PROC_ALL_PIDS = 1
_PROC_PIDTBSDINFO = 3
_CTL_KERN = 1
_KERN_PROCARGS2 = 49
# ``pbi_status`` values from <sys/proc.h>, mapped to the ``ps`` state letters the
# rest of process_tree already understands.
_STATE = {1: "I", 2: "R", 3: "S", 4: "T", 5: "Z"}


class _ProcBsdInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


def available() -> bool:
    return sys.platform == "darwin"


def _libproc() -> ctypes.CDLL:
    lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    lib.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
    lib.proc_listpids.restype = ctypes.c_int
    lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    lib.proc_pidinfo.restype = ctypes.c_int
    return lib


def list_pids() -> list[int]:
    lib = _libproc()
    size = lib.proc_listpids(_PROC_ALL_PIDS, 0, None, 0)
    if size <= 0:
        return []
    buf = (ctypes.c_int * (size // ctypes.sizeof(ctypes.c_int) + 64))()
    got = lib.proc_listpids(_PROC_ALL_PIDS, 0, buf, ctypes.sizeof(buf))
    return [pid for pid in buf[: max(got, 0) // ctypes.sizeof(ctypes.c_int)] if pid > 0]


def bsd_info(pid: int) -> _ProcBsdInfo | None:
    lib = _libproc()
    info = _ProcBsdInfo()
    read = lib.proc_pidinfo(pid, _PROC_PIDTBSDINFO, 0, ctypes.byref(info), ctypes.sizeof(info))
    return info if read == ctypes.sizeof(info) else None


def process_rows() -> dict[int, tuple[int, int, str | None, str | None]]:
    """pid -> (ppid, pgid, start identity, state letter) for every visible process."""
    rows: dict[int, tuple[int, int, str | None, str | None]] = {}
    for pid in list_pids():
        info = bsd_info(pid)
        if info is None:
            continue
        rows[pid] = (
            int(info.pbi_ppid),
            int(info.pbi_pgid),
            f"darwin:{info.pbi_start_tvsec}:{info.pbi_start_tvusec}",
            _STATE.get(int(info.pbi_status)),
        )
    return rows


def argv(pid: int) -> list[bytes]:
    """The argument vector of *pid*, empty when the kernel refuses or the process is gone."""
    libc = ctypes.CDLL(None, use_errno=True)
    mib = (ctypes.c_int * 3)(_CTL_KERN, _KERN_PROCARGS2, pid)
    size = ctypes.c_size_t(0)
    if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0 or size.value < 4:
        return []
    buf = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
        return []
    raw = buf.raw[: size.value]
    argc = int.from_bytes(raw[:4], sys.byteorder)
    rest = raw[4:]
    # executable path, then NUL padding, then argc NUL-terminated arguments
    end = rest.find(b"\0")
    if end < 0:
        return []
    rest = rest[end:].lstrip(b"\0")
    parts = rest.split(b"\0")
    return parts[:argc]


def pids_with_argv(needle: bytes) -> list[int]:
    """Every process whose argument vector contains an argument equal to *needle*."""
    return [pid for pid in list_pids() if needle in argv(pid)]
