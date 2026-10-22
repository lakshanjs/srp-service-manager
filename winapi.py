"""Direct Win32 calls for the things the app used to shell out for.

`sc query` every few seconds and a PowerShell/WMI process listing before every start each
cost a process launch (tens of milliseconds, or most of a second for PowerShell); these
answer the same questions in microseconds to milliseconds, without spawning anything."""

import os
import ctypes
from ctypes import wintypes

advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")

# ------------------------------------------------------------------ services

SC_MANAGER_CONNECT = 0x0001
SERVICE_QUERY_CONFIG = 0x0001
SERVICE_QUERY_STATUS = 0x0004
SC_STATUS_PROCESS_INFO = 0
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_SERVICE_DOES_NOT_EXIST = 1060

SERVICE_STATES = {1: "stopped", 2: "pending", 3: "pending", 4: "running",
                  5: "pending", 6: "pending"}  # 7 (paused) falls through to "unknown"


class SERVICE_STATUS_PROCESS(ctypes.Structure):
    _fields_ = [("dwServiceType", wintypes.DWORD), ("dwCurrentState", wintypes.DWORD),
                ("dwControlsAccepted", wintypes.DWORD), ("dwWin32ExitCode", wintypes.DWORD),
                ("dwServiceSpecificExitCode", wintypes.DWORD), ("dwCheckPoint", wintypes.DWORD),
                ("dwWaitHint", wintypes.DWORD), ("dwProcessId", wintypes.DWORD),
                ("dwServiceFlags", wintypes.DWORD)]


class QUERY_SERVICE_CONFIGW(ctypes.Structure):
    _fields_ = [("dwServiceType", wintypes.DWORD), ("dwStartType", wintypes.DWORD),
                ("dwErrorControl", wintypes.DWORD), ("lpBinaryPathName", wintypes.LPWSTR),
                ("lpLoadOrderGroup", wintypes.LPWSTR), ("dwTagId", wintypes.DWORD),
                ("lpDependencies", wintypes.LPWSTR), ("lpServiceStartName", wintypes.LPWSTR),
                ("lpDisplayName", wintypes.LPWSTR)]


advapi32.OpenSCManagerW.restype = wintypes.HANDLE
advapi32.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
advapi32.OpenServiceW.restype = wintypes.HANDLE
advapi32.OpenServiceW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD]
advapi32.CloseServiceHandle.argtypes = [wintypes.HANDLE]
advapi32.QueryServiceStatusEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                          wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
advapi32.QueryServiceConfigW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                         ctypes.POINTER(wintypes.DWORD)]


def _open_service(name, access):
    """(scm, service) handles, or raises LookupError if there's no such service."""
    scm = advapi32.OpenSCManagerW(None, None, SC_MANAGER_CONNECT)
    if not scm:
        raise OSError(ctypes.get_last_error(), "OpenSCManager failed")
    svc = advapi32.OpenServiceW(scm, name, access)
    if not svc:
        err = ctypes.get_last_error()
        advapi32.CloseServiceHandle(scm)
        if err == ERROR_SERVICE_DOES_NOT_EXIST:
            raise LookupError(name)
        raise OSError(err, "OpenService failed")
    return scm, svc


def _close(scm, svc):
    advapi32.CloseServiceHandle(svc)
    advapi32.CloseServiceHandle(scm)


def service_status(name):
    """(state, pid) of a Windows service; state is 'running' | 'stopped' | 'pending' |
    'not_found' | 'unknown', and pid is 0 unless it's running. Needs no admin rights."""
    try:
        scm, svc = _open_service(name, SERVICE_QUERY_STATUS)
    except LookupError:
        return "not_found", 0
    except OSError:
        return "unknown", 0
    try:
        status = SERVICE_STATUS_PROCESS()
        needed = wintypes.DWORD()
        if not advapi32.QueryServiceStatusEx(svc, SC_STATUS_PROCESS_INFO, ctypes.byref(status),
                                             ctypes.sizeof(status), ctypes.byref(needed)):
            return "unknown", 0
        return SERVICE_STATES.get(status.dwCurrentState, "unknown"), status.dwProcessId
    finally:
        _close(scm, svc)


def service_binary_path(name):
    """A service's raw BINARY_PATH_NAME (path plus any arguments), or None."""
    try:
        scm, svc = _open_service(name, SERVICE_QUERY_CONFIG)
    except (LookupError, OSError):
        return None
    try:
        needed = wintypes.DWORD()
        advapi32.QueryServiceConfigW(svc, None, 0, ctypes.byref(needed))
        if ctypes.get_last_error() != ERROR_INSUFFICIENT_BUFFER:
            return None
        buf = ctypes.create_string_buffer(needed.value)
        if not advapi32.QueryServiceConfigW(svc, buf, needed, ctypes.byref(needed)):
            return None
        return ctypes.cast(buf, ctypes.POINTER(QUERY_SERVICE_CONFIGW)).contents.lpBinaryPathName
    finally:
        _close(scm, svc)


# ----------------------------------------------------------------- processes

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ProcessCommandLineInformation = 60  # Windows 8.1+
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT),
                ("Buffer", ctypes.c_void_p)]


kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
ntdll.NtQueryInformationProcess.restype = ctypes.c_long
ntdll.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                            wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]


def _snapshot():
    """(pid, ppid, image name) for every process."""
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE:
        return []
    rows = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            rows.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return rows


def _image_and_cmdline(pid):
    """Full executable path and command line, or "" for whatever we're not allowed to read
    (protected processes; most other users' processes give us both)."""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return "", ""
    try:
        size = wintypes.DWORD(32768)
        path = ctypes.create_unicode_buffer(size.value)
        exe = path.value if kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)) else ""

        needed = wintypes.ULONG()
        ntdll.NtQueryInformationProcess(handle, ProcessCommandLineInformation, None, 0,
                                        ctypes.byref(needed))
        cmdline = ""
        if needed.value:
            buf = ctypes.create_string_buffer(needed.value)
            if ntdll.NtQueryInformationProcess(handle, ProcessCommandLineInformation, buf,
                                               needed, ctypes.byref(needed)) == 0:
                us = UNICODE_STRING.from_buffer(buf)
                if us.Buffer and us.Length:
                    cmdline = ctypes.wstring_at(us.Buffer, us.Length // 2)
        return exe, cmdline
    finally:
        kernel32.CloseHandle(handle)


def list_processes(image=None):
    """Running processes as {pid, ppid, exe, cmdline}.

    `image` (an executable path or bare name, e.g. "mysqld.exe") limits the result to
    processes of that image name. Full path and command line are only read for those, which
    is what makes a filtered call cheap. Returns [] if the snapshot fails, so a lookup
    problem never blocks a start."""
    want = os.path.basename(image).lower() if image else None
    procs = []
    for pid, ppid, name in _snapshot():
        if want and name.lower() != want:
            continue
        exe, cmdline = _image_and_cmdline(pid) if pid else ("", "")
        procs.append({"pid": pid, "ppid": ppid, "exe": exe, "cmdline": cmdline})
    return procs
