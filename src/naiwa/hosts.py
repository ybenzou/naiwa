"""Read Windows process identity only; never IDE logs, transcripts or databases."""
from __future__ import annotations

import ctypes
import os
import re

EDITORS = {"code.exe", "code-insiders.exe"}


def _processes():
    if os.name != "nt":
        return {}
    import ctypes.wintypes as w
    class Entry(ctypes.Structure):
        _fields_ = [("size", w.DWORD), ("usage", w.DWORD), ("pid", w.DWORD),
                    ("heap", ctypes.c_size_t), ("module", w.DWORD), ("threads", w.DWORD),
                    ("parent", w.DWORD), ("priority", w.LONG), ("flags", w.DWORD),
                    ("exe", w.WCHAR*260)]
    kernel = ctypes.windll.kernel32
    kernel.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = w.HANDLE
    kernel.Process32FirstW.argtypes = kernel.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(Entry)]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        return {}
    result = {}
    try:
        entry = Entry(); entry.size = ctypes.sizeof(entry)
        present = kernel.Process32FirstW(handle, ctypes.byref(entry))
        while present:
            result[entry.pid] = (entry.parent, entry.exe.casefold())
            present = kernel.Process32NextW(handle, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(handle)
    return result


def _is_host(pid, processes):
    parent, name = processes.get(pid, (0, ""))
    return name == "codex.exe" and processes.get(parent, (0, ""))[1] in EDITORS


def _describe(pid):
    import ctypes.wintypes as w
    kernel = ctypes.windll.kernel32
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
    kernel.GetProcessTimes.argtypes = [w.HANDLE]+[ctypes.POINTER(w.FILETIME)]*4
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return {}
    try:
        path = ctypes.create_unicode_buffer(32768); size = w.DWORD(len(path))
        if not kernel.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
            return {}
        version = re.search(r"[\\/]openai\.chatgpt-([^\\/]+)[\\/]", path.value, re.IGNORECASE)
        if not version:
            return {}
        created, exited, system, user = (w.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                      ctypes.byref(system), ctypes.byref(user)):
            return {}
        started = ((created.dwHighDateTime << 32)+created.dwLowDateTime)/10_000_000-11_644_473_600
        return {"pid": pid, "started_at": started, "extension_version": version.group(1)}
    finally:
        kernel.CloseHandle(handle)


def producer_host():
    """Correlate this hook with its app-server through its parent chain."""
    processes = _processes()
    pid = os.getppid()
    seen = set()
    while pid and pid not in seen:
        if _is_host(pid, processes):
            return _describe(pid)
        seen.add(pid)
        pid = processes.get(pid, (0, ""))[0]
    return {}


def running_hosts():
    processes = _processes()
    return [host for pid in processes if _is_host(pid, processes) if (host := _describe(pid))]


def coverage(rows):
    """A running instance is verified only by a hook from this exact process life."""
    result = []
    for host in running_hosts():
        observations = [row for row in rows if row.get("source") == "codex" and row.get("origin") == "vscode"
                        and row.get("observed") and row.get("producer") == host]
        result.append({**host, "observer_seen": bool(observations),
                       "last_observed": max((row.get("ts", "") for row in observations), default=""),
                       "workspaces": sorted({row["workspace_name"] for row in observations if row.get("workspace_name")})})
    return result
