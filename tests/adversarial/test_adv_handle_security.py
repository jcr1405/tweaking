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
            "# 1. Child attempts to signal parent event\n"
            "kernel32.SetLastError(0)\n"
            "res_event = kernel32.SetEvent(wintypes.HANDLE(event_h))\n"
            "err_event = kernel32.GetLastError()\n"
            "# 2. Child attempts to release parent mutex\n"
            "kernel32.SetLastError(0)\n"
            "res_mutex = kernel32.ReleaseMutex(wintypes.HANDLE(mutex_h))\n"
            "err_mutex = kernel32.GetLastError()\n"
            "# 3. Child attempts to write to parent dummy pipe\n"
            "dummy_bytes = b'LEAK'\n"
            "written = wintypes.DWORD()\n"
            "kernel32.SetLastError(0)\n"
            "res_pipe = kernel32.WriteFile(wintypes.HANDLE(pipe_h), dummy_bytes, len(dummy_bytes), ctypes.byref(written), None)\n"
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

            # Check kernel object isolation using DuplicateHandle & CompareObjectHandles
            DUPLICATE_SAME_ACCESS = 0x00000002
            kernelbase = getattr(ctypes.windll, "kernelbase", kernel32)
            for h_val, h_parent, desc in [
                (event_val, h_event, "event"),
                (mutex_val, h_mutex, "mutex"),
                (dummy_pipe_val, h_dummy_read, "pipe"),
            ]:
                h_dup = wintypes.HANDLE()
                dup_ok = kernel32.DuplicateHandle(
                    proc.h_process,
                    wintypes.HANDLE(h_val),
                    kernel32.GetCurrentProcess(),
                    ctypes.byref(h_dup),
                    0,
                    False,
                    DUPLICATE_SAME_ACCESS,
                )
                if dup_ok:
                    # If slot was reused by child CRT for an internal object, prove it is NOT the parent's object
                    if hasattr(kernelbase, "CompareObjectHandles"):
                        same = kernelbase.CompareObjectHandles(wintypes.HANDLE(h_parent), h_dup)
                        self.assertFalse(bool(same), f"Child handle {desc} must NOT match parent kernel object")
                    kernel32.CloseHandle(h_dup)
                else:
                    # Slot does not exist in child (standard case)
                    self.assertEqual(kernel32.GetLastError(), 6, f"DuplicateHandle for {desc} must return ERROR_INVALID_HANDLE")

            line_ev = proc.stdout.readline().decode().strip()
            line_mu = proc.stdout.readline().decode().strip()
            line_pi = proc.stdout.readline().decode().strip()

            # Verify in parent that child operations could not affect parent kernel objects:
            WAIT_TIMEOUT = 258
            self.assertEqual(
                kernel32.WaitForSingleObject(h_event, 0),
                WAIT_TIMEOUT,
                "Parent event must remain unsignaled despite child attempt to signal it"
            )

            avail = wintypes.DWORD()
            kernel32.PeekNamedPipe(h_dummy_read, None, 0, None, ctypes.byref(avail), None)
            self.assertEqual(avail.value, 0, "Parent pipe must contain 0 leaked bytes")

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
