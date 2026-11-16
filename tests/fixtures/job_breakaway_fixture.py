"""Adversarial Process Tree & Job Breakaway Fixture
Native Windows test fixture that attempts to spawn child and grandchild processes
with CREATE_BREAKAWAY_FROM_JOB (0x01000000) under various Job Object configurations.
Reports native Win32 errors, process IDs, and IsProcessInJob membership status.
"""

import sys
import os
import json
import ctypes
from ctypes import wintypes

# Win32 Constants
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000
JOB_OBJECT_QUERY = 0x0004
STILL_ACTIVE = 259
WAIT_TIMEOUT = 0x00000102

class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]

class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]

def check_is_in_job(h_process, h_job) -> bool:
    """Invokes Win32 IsProcessInJob and returns bool."""
    if not h_process or not h_job:
        return False
    kernel32 = ctypes.windll.kernel32
    is_in = wintypes.BOOL()
    ok = kernel32.IsProcessInJob(h_process, h_job, ctypes.byref(is_in))
    return bool(ok and is_in.value)

def main():
    if sys.platform != "win32":
        print(json.dumps({"error": "Not a Windows platform"}))
        return

    kernel32 = ctypes.windll.kernel32
    kernel32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    kernel32.IsProcessInJob.restype = wintypes.BOOL
    kernel32.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPWSTR]
    kernel32.OpenJobObjectW.restype = wintypes.HANDLE

    my_pid = kernel32.GetCurrentProcessId()
    h_current_process = kernel32.GetCurrentProcess()

    outer_job_name = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] != "none" else None
    inner_job_name = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] != "none" else None
    mode = sys.argv[3] if len(sys.argv) > 3 else "attempt_breakaway"

    h_outer_job = kernel32.OpenJobObjectW(JOB_OBJECT_QUERY, False, outer_job_name) if outer_job_name else None
    h_inner_job = kernel32.OpenJobObjectW(JOB_OBJECT_QUERY, False, inner_job_name) if inner_job_name else None

    # Check initial membership of this process
    in_outer = check_is_in_job(h_current_process, h_outer_job) if h_outer_job else None
    in_inner = check_is_in_job(h_current_process, h_inner_job) if h_inner_job else None

    result = {
        "fixture_pid": my_pid,
        "outer_job_name": outer_job_name,
        "inner_job_name": inner_job_name,
        "fixture_in_outer": in_outer,
        "fixture_in_inner": in_inner,
        "child_pid": None,
        "breakaway_requested": True,
        "create_process_success": False,
        "win32_last_error": 0,
        "child_in_outer": None,
        "child_in_inner": None
    }

    if mode == "attempt_breakaway":
        # Attempt to spawn child with CREATE_BREAKAWAY_FROM_JOB
        cmdline = f'"{sys.executable}" -c "import time; time.sleep(10)"'
        si = STARTUPINFOW()
        si.cb = ctypes.sizeof(STARTUPINFOW)
        pi = PROCESS_INFORMATION()

        flags = CREATE_BREAKAWAY_FROM_JOB | CREATE_NO_WINDOW
        success = kernel32.CreateProcessW(
            None,
            ctypes.create_unicode_buffer(cmdline),
            None,
            None,
            False,
            flags,
            None,
            None,
            ctypes.byref(si),
            ctypes.byref(pi)
        )

        if success:
            result["create_process_success"] = True
            result["child_pid"] = pi.dwProcessId
            if h_outer_job:
                result["child_in_outer"] = check_is_in_job(pi.hProcess, h_outer_job)
            if h_inner_job:
                result["child_in_inner"] = check_is_in_job(pi.hProcess, h_inner_job)
            # Close handles to child
            kernel32.CloseHandle(pi.hThread)
            kernel32.CloseHandle(pi.hProcess)
        else:
            result["create_process_success"] = False
            result["win32_last_error"] = kernel32.GetLastError()

    # Close job query handles
    if h_outer_job:
        kernel32.CloseHandle(h_outer_job)
    if h_inner_job:
        kernel32.CloseHandle(h_inner_job)

    # Output JSON result on stdout
    sys.stdout.write(json.dumps(result) + "\n")
    sys.stdout.flush()

if __name__ == "__main__":
    main()
