"""Adversarial Verification Suite: Handle Inheritance Security
Tests:
1. Adversarial parent handle leak attempt:
   Parent creates inheritable file, event, mutex, and pipe handles (bInheritHandle = True).
2. Child process attempts to read/use those parent handle values.
3. Asserts that the Windows kernel restricted handle inheritance strictly to the 3 stdio pipes,
   and any attempt to use the parent handles in the child returns ERROR_INVALID_HANDLE (6).
"""

import sys
import os
import ctypes
from ctypes import wintypes
import unittest

from runtime.platform_win32 import (
    Win32JobObject,
    Win32SuspendedLauncher,
    SECURITY_ATTRIBUTES,
)


@unittest.skipUnless(sys.platform == "win32", "Win32 handle inheritance tests require Windows")
class TestAdvHandleSecurity(unittest.TestCase):

    def test_unrelated_inheritable_parent_handles_not_inherited(self):
        """Verifies that PROC_THREAD_ATTRIBUTE_HANDLE_LIST prevents leaking inheritable parent handles into child."""
        kernel32 = ctypes.windll.kernel32

        # 1. Create inheritable security attributes
        sa = SECURITY_ATTRIBUTES()
        sa.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
        sa.bInheritHandle = True
        sa.lpSecurityDescriptor = None

        # 2. Create an inheritable Event
        h_event = kernel32.CreateEventW(ctypes.byref(sa), True, False, None)
        self.assertTrue(bool(h_event), f"Failed to create event: {kernel32.GetLastError()}")

        # 3. Create an inheritable Mutex
        h_mutex = kernel32.CreateMutexW(ctypes.byref(sa), False, None)
        self.assertTrue(bool(h_mutex), f"Failed to create mutex: {kernel32.GetLastError()}")

        # 4. Create an inheritable Dummy Pipe
        h_dummy_read = wintypes.HANDLE()
        h_dummy_write = wintypes.HANDLE()
        self.assertTrue(kernel32.CreatePipe(ctypes.byref(h_dummy_read), ctypes.byref(h_dummy_write), ctypes.byref(sa), 0))

        event_val = int(h_event) if not hasattr(h_event, "value") else h_event.value
        mutex_val = int(h_mutex) if not hasattr(h_mutex, "value") else h_mutex.value
        dummy_pipe_val = int(h_dummy_read) if not hasattr(h_dummy_read, "value") else h_dummy_read.value

        job = Win32JobObject(name="AdvTestHandleLeakJob")
        job.create(kill_on_close=True)

        env = {
            "SystemRoot": os.environ.get("SystemRoot", r"C:\Windows"),
            "PYTHONUNBUFFERED": "1",
        }

        # Child tries to inspect and use those handle integer values
        code = (
            "import sys, ctypes\n"
            "from ctypes import wintypes\n"
            "kernel32 = ctypes.windll.kernel32\n"
            "event_h = int(sys.argv[1])\n"
            "mutex_h = int(sys.argv[2])\n"
            "pipe_h = int(sys.argv[3])\n"
            "flags = wintypes.DWORD()\n"
            "kernel32.SetLastError(0)\n"
            "res_event = kernel32.GetHandleInformation(wintypes.HANDLE(event_h), ctypes.byref(flags))\n"
            "err_event = kernel32.GetLastError()\n"
            "kernel32.SetLastError(0)\n"
            "res_mutex = kernel32.GetHandleInformation(wintypes.HANDLE(mutex_h), ctypes.byref(flags))\n"
            "err_mutex = kernel32.GetLastError()\n"
            "kernel32.SetLastError(0)\n"
            "res_pipe = kernel32.GetHandleInformation(wintypes.HANDLE(pipe_h), ctypes.byref(flags))\n"
            "err_pipe = kernel32.GetLastError()\n"
            "print(f'EVENT_RES={res_event},ERR={err_event}', flush=True)\n"
            "print(f'MUTEX_RES={res_mutex},ERR={err_mutex}', flush=True)\n"
            "print(f'PIPE_RES={res_pipe},ERR={err_pipe}', flush=True)\n"
        )

        try:
            proc = Win32SuspendedLauncher.launch(
                command=sys.executable,
                args=["-c", code, str(event_val), str(mutex_val), str(dummy_pipe_val)],
                env=env,
                job=job,
            )

            line_ev = proc.stdout.readline().decode().strip()
            line_mu = proc.stdout.readline().decode().strip()
            line_pi = proc.stdout.readline().decode().strip()

            # Win32 GetHandleInformation returns 0 with error 6 (ERROR_INVALID_HANDLE) when handle is not present in process
            self.assertEqual(line_ev, "EVENT_RES=0,ERR=6", "Parent event handle must be INVALID_HANDLE in child")
            self.assertEqual(line_mu, "MUTEX_RES=0,ERR=6", "Parent mutex handle must be INVALID_HANDLE in child")
            self.assertEqual(line_pi, "PIPE_RES=0,ERR=6", "Parent dummy pipe handle must be INVALID_HANDLE in child")

            self.assertEqual(proc.wait(timeout_ms=5000), 0)
            proc.close()
        finally:
            kernel32.CloseHandle(h_event)
            kernel32.CloseHandle(h_mutex)
            kernel32.CloseHandle(h_dummy_read)
            kernel32.CloseHandle(h_dummy_write)
            job.close()


if __name__ == "__main__":
    unittest.main()
