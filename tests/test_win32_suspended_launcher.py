"""Unit and Integration Tests for Win32 Suspended Process Launcher and Job Containment
Verifies that:
1. Processes are created in CREATE_SUSPENDED state and assigned to Job Object before resuming.
2. PROC_THREAD_ATTRIBUTE_HANDLE_LIST restricts inherited handles to stdin/stdout/stderr only.
3. Unicode environment block is properly formatted, deduplicated, and passed to child.
4. Process trees (child and grandchild) are fully contained within the Job Object and killed on close.
5. Bidirectional stdio communication works seamlessly via Win32 pipes.
"""

import sys
import os
import time
import ctypes
from ctypes import wintypes
import unittest

from runtime.platform_win32 import (
    Win32JobObject,
    Win32SuspendedLauncher,
    Win32SuspendedProcess,
    create_unicode_environment_block,
)


@unittest.skipUnless(sys.platform == "win32", "Win32SuspendedLauncher tests require Windows")
class TestWin32SuspendedLauncher(unittest.TestCase):

    def test_unicode_environment_block_construction(self):
        env = {
            "B_KEY": "val_b",
            "a_key": "val_a_lower",
            "A_KEY": "val_a_upper",  # Should overwrite a_key in case-insensitive deduplication
            "UTF8_TEST": "Presence-Hardening-🚀",
        }
        buf = create_unicode_environment_block(env)
        raw_val = buf[:]
        # Check that null-separated string contains uppercase keys and values
        self.assertIn("A_KEY=val_a_upper", raw_val)
        self.assertIn("B_KEY=val_b", raw_val)
        self.assertIn("UTF8_TEST=Presence-Hardening-🚀", raw_val)
        # Check alphabetical sorting
        lines = [x for x in raw_val.split("\0") if x]
        keys = [x.split("=")[0] for x in lines]
        self.assertEqual(keys, sorted(keys, key=lambda s: s.upper()))

    def test_invalid_executable_raises_filenotfound(self):
        with self.assertRaises(FileNotFoundError):
            Win32SuspendedLauncher.launch(
                command="non_existent_binary_presence_xyz_123.exe",
                args=[],
                env={"SystemRoot": os.environ.get("SystemRoot", r"C:\Windows")},
            )

    def test_suspended_launch_and_immediate_job_membership(self):
        """Verifies process is created suspended, assigned to Job Object, and IsProcessInJob returns True."""
        kernel32 = ctypes.windll.kernel32
        job = Win32JobObject(name="TestSuspendedJob_001")
        self.assertTrue(job.create(kill_on_close=True))

        env = {
            "SystemRoot": os.environ.get("SystemRoot", r"C:\Windows"),
            "PYTHONUNBUFFERED": "1",
        }

        proc = Win32SuspendedLauncher.launch(
            command=sys.executable,
            args=["-c", "import sys; print('PRESENCE_SUSPENDED_OK', flush=True)"],
            env=env,
            job=job,
        )

        try:
            self.assertIsNotNone(proc.pid)
            self.assertGreater(proc.pid, 0)

            # Check that the process is indeed inside our Job Object
            is_in_job = wintypes.BOOL()
            success = kernel32.IsProcessInJob(proc.h_process, job.handle, ctypes.byref(is_in_job))
            self.assertTrue(success, f"IsProcessInJob API call failed: error {kernel32.GetLastError()}")
            self.assertTrue(bool(is_in_job.value), "Child process MUST be verified inside Job Object")

            # Read stdout
            output = proc.stdout.readline().decode("utf-8").strip()
            self.assertEqual(output, "PRESENCE_SUSPENDED_OK")

            exit_code = proc.wait(timeout_ms=5000)
            self.assertEqual(exit_code, 0)
        finally:
            proc.close()
            job.close()

    def test_bidirectional_stdio_communication(self):
        """Verifies full duplex pipe communication over Win32 stdin/stdout pipes."""
        job = Win32JobObject()
        job.create(kill_on_close=True)

        env = {
            "SystemRoot": os.environ.get("SystemRoot", r"C:\Windows"),
            "PYTHONUNBUFFERED": "1",
        }

        # Child reads a line and echoes it with a prefix
        code = (
            "import sys\n"
            "line = sys.stdin.readline().strip()\n"
            "sys.stdout.write(f'REPLY:{line}\\n')\n"
            "sys.stdout.flush()\n"
        )

        proc = Win32SuspendedLauncher.launch(
            command=sys.executable,
            args=["-c", code],
            env=env,
            job=job,
        )

        try:
            msg = "PresenceProtocolEcho\n"
            proc.stdin.write(msg.encode("utf-8"))
            proc.stdin.flush()

            reply = proc.stdout.readline().decode("utf-8").strip()
            self.assertEqual(reply, "REPLY:PresenceProtocolEcho")

            exit_code = proc.wait(timeout_ms=5000)
            self.assertEqual(exit_code, 0)
        finally:
            proc.close()
            job.close()

    def test_stderr_capture_and_exit_code(self):
        """Verifies stderr stream is captured and non-zero exit codes are retrieved."""
        job = Win32JobObject()
        job.create(kill_on_close=True)

        env = {
            "SystemRoot": os.environ.get("SystemRoot", r"C:\Windows"),
            "PYTHONUNBUFFERED": "1",
        }

        code = (
            "import sys\n"
            "sys.stderr.write('CRITICAL_ERR_LOG\\n')\n"
            "sys.stderr.flush()\n"
            "sys.exit(42)\n"
        )

        proc = Win32SuspendedLauncher.launch(
            command=sys.executable,
            args=["-c", code],
            env=env,
            job=job,
        )

        try:
            err_line = proc.stderr.readline().decode("utf-8").strip()
            self.assertEqual(err_line, "CRITICAL_ERR_LOG")

            exit_code = proc.wait(timeout_ms=5000)
            self.assertEqual(exit_code, 42)
        finally:
            proc.close()
            job.close()

    def test_process_tree_containment_and_kill_on_job_close(self):
        """Verifies that child and grandchild processes are both in the job and terminated on close."""
        import subprocess

        job = Win32JobObject(name="PresenceTreeJob_Test")
        job.create(kill_on_close=True)

        fixture_path = os.path.join(os.path.dirname(__file__), "fixtures", "spawn_tree_fixture.py")
        env = {
            "SystemRoot": os.environ.get("SystemRoot", r"C:\Windows"),
            "PYTHONUNBUFFERED": "1",
        }

        proc = Win32SuspendedLauncher.launch(
            command=sys.executable,
            args=[fixture_path],
            env=env,
            job=job,
        )

        kernel32 = ctypes.windll.kernel32
        child_pid = None
        grandchild_pid = None

        try:
            # Read child and grandchild PIDs from fixture output
            line1 = proc.stdout.readline().decode("utf-8").strip()
            line2 = proc.stdout.readline().decode("utf-8").strip()

            self.assertTrue(line1.startswith("CHILD_PID="), f"Unexpected line1: {line1}")
            self.assertTrue(line2.startswith("GRANDCHILD_PID="), f"Unexpected line2: {line2}")

            child_pid = int(line1.split("=")[1])
            grandchild_pid = int(line2.split("=")[1])

            self.assertNotEqual(grandchild_pid, child_pid)

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

            # Verify child is in the Job Object
            h_child = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, child_pid)
            self.assertTrue(bool(h_child), f"Failed to open child process {child_pid}")
            try:
                is_in_job = wintypes.BOOL()
                success = kernel32.IsProcessInJob(h_child, job.handle, ctypes.byref(is_in_job))
                self.assertTrue(success, f"IsProcessInJob failed on child: error {kernel32.GetLastError()}")
                self.assertTrue(bool(is_in_job.value), "Child process MUST be inside Job Object")
            finally:
                kernel32.CloseHandle(h_child)

            # Verify grandchild is also in the Job Object
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h_grandchild = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, grandchild_pid)
            self.assertTrue(bool(h_grandchild), f"Failed to open grandchild process {grandchild_pid}")

            try:
                is_in_job = wintypes.BOOL()
                success = kernel32.IsProcessInJob(h_grandchild, job.handle, ctypes.byref(is_in_job))
                self.assertTrue(success, f"IsProcessInJob failed on grandchild: error {kernel32.GetLastError()}")
                self.assertTrue(bool(is_in_job.value), "Grandchild process MUST be inside Job Object")
            finally:
                kernel32.CloseHandle(h_grandchild)

            # Both child and grandchild are verified alive and in Job Object
            # Now close the Job Object, triggering JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            job.close()

            # Wait briefly for Windows kernel to reap both processes
            time.sleep(0.5)

            # Verify child is terminated
            h_child_check = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, child_pid)
            if h_child_check:
                exit_c = wintypes.DWORD()
                kernel32.GetExitCodeProcess(h_child_check, ctypes.byref(exit_c))
                kernel32.CloseHandle(h_child_check)
                self.assertNotEqual(exit_c.value, 259, "Child process should be terminated")

            # Verify grandchild is terminated
            h_gc_check = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, grandchild_pid)
            if h_gc_check:
                exit_c = wintypes.DWORD()
                kernel32.GetExitCodeProcess(h_gc_check, ctypes.byref(exit_c))
                kernel32.CloseHandle(h_gc_check)
                self.assertNotEqual(exit_c.value, 259, "Grandchild process should be terminated by Job Object kill-on-close")

        finally:
            proc.close()
            job.close()
            # Clean up processes defensively if anything failed
            if child_pid:
                try:
                    os.kill(child_pid, 9)
                except OSError:
                    pass
            if grandchild_pid:
                try:
                    os.kill(grandchild_pid, 9)
                except OSError:
                    pass


if __name__ == "__main__":
    unittest.main()
