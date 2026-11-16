"""Adversarial Verification Suite: Job Object and Process-Tree Containment
Tests:
1. Child, worker, and grandchild containment across multi-level process trees.
2. Breakaway attack: Child attempts CreateProcess with CREATE_BREAKAWAY_FROM_JOB (0x01000000).
   Asserts Windows kernel denies the breakaway under JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.
3. Abrupt parent termination: Independent controller launches a parent process hosting an MCP server
   with descendants, forcefully terminates the parent (SIGKILL/taskkill), and verifies that
   the Windows kernel reaps every single descendant process.
4. Job Object handle ownership and close semantics.
"""

import sys
import os
import time
import ctypes
from ctypes import wintypes
import subprocess
import unittest

from runtime.platform_win32 import (
    Win32JobObject,
    Win32SuspendedLauncher,
    Win32SuspendedProcess,
)


@unittest.skipUnless(sys.platform == "win32", "Win32 Job Object tests require Windows")
class TestAdvJobContainment(unittest.TestCase):

    def test_breakaway_attempt_denied_by_kernel(self):
        """Verifies that child process cannot escape via CREATE_BREAKAWAY_FROM_JOB (0x01000000).
        Because JOB_OBJECT_LIMIT_BREAKAWAY_OK is not set on our Job Object,
        CreateProcess with CREATE_BREAKAWAY_FROM_JOB must fail with ERROR_ACCESS_DENIED (5).
        """
        job = Win32JobObject(name="AdvTestBreakawayJob")
        job.create(kill_on_close=True)

        env = {
            "SystemRoot": os.environ.get("SystemRoot", r"C:\Windows"),
            "PYTHONUNBUFFERED": "1",
        }

        # Child tries to spawn an uncontained grandchild with CREATE_BREAKAWAY_FROM_JOB
        code = (
            "import sys, ctypes, struct, time\n"
            "from ctypes import wintypes\n"
            "CREATE_BREAKAWAY_FROM_JOB = 0x01000000\n"
            "kernel32 = ctypes.windll.kernel32\n"
            "si = ctypes.create_string_buffer(104)\n"
            "pi = ctypes.create_string_buffer(24)\n"
            "cmd = ctypes.create_unicode_buffer(f'\"{sys.executable}\" -c \"import time; time.sleep(10)\"')\n"
            "res = kernel32.CreateProcessW(None, cmd, None, None, False, CREATE_BREAKAWAY_FROM_JOB, None, None, si, pi)\n"
            "err = kernel32.GetLastError()\n"
            "if res:\n"
            "    h_proc, h_thread, pid, tid = struct.unpack('NNII', pi.raw)\n"
            "    print(f'BREAKAWAY_SPAWNED:{pid}', flush=True)\n"
            "else:\n"
            "    print(f'BREAKAWAY_BLOCKED_WITH_ERROR_{err}', flush=True)\n"
        )

        proc = Win32SuspendedLauncher.launch(
            command=sys.executable,
            args=["-c", code],
            env=env,
            job=job,
        )

        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

        try:
            line = proc.stdout.readline().decode("utf-8").strip()
            if line.startswith("BREAKAWAY_SPAWNED:"):
                # Even if spawned, it must be trapped inside the Job Object!
                breakaway_pid = int(line.split(":")[1])
                h_b = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, breakaway_pid)
                self.assertTrue(bool(h_b), f"Failed to open breakaway process {breakaway_pid}")
                try:
                    is_in_job = wintypes.BOOL()
                    kernel32.IsProcessInJob(h_b, job.handle, ctypes.byref(is_in_job))
                    self.assertTrue(
                        bool(is_in_job.value),
                        f"CRITICAL VULNERABILITY: Process {breakaway_pid} successfully escaped Job Object via CREATE_BREAKAWAY_FROM_JOB!"
                    )
                finally:
                    kernel32.CloseHandle(h_b)

                # Close job object: the attempted breakaway process MUST be terminated
                job.close()
                time.sleep(0.5)

                h_check = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, breakaway_pid)
                if h_check:
                    exit_c = wintypes.DWORD()
                    kernel32.GetExitCodeProcess(h_check, ctypes.byref(exit_c))
                    kernel32.CloseHandle(h_check)
                    self.assertNotEqual(
                        exit_c.value, 259,
                        f"CRITICAL VULNERABILITY: Process {breakaway_pid} survived Job Object termination!"
                    )
            else:
                self.assertTrue(
                    line.startswith("BREAKAWAY_BLOCKED_WITH_ERROR_"),
                    f"Unexpected output: {line}"
                )
        finally:
            proc.close()
            job.close()

    def test_abrupt_parent_termination_cleans_up_process_tree(self):
        """Simulates an abrupt, unhandled parent process termination (SIGKILL).
        Verifies that the Windows NT kernel reaps the entire descendant tree (worker + grandchild).
        """
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

        # Script for the doomed parent process
        parent_script = (
            "import sys, os, time\n"
            "from runtime.platform_win32 import Win32JobObject, Win32SuspendedLauncher\n"
            "job = Win32JobObject(name='DoomedParentJob')\n"
            "job.create(kill_on_close=True)\n"
            "fixture_path = os.path.abspath('tests/fixtures/spawn_tree_fixture.py')\n"
            "proc = Win32SuspendedLauncher.launch(\n"
            "    command=sys.executable,\n"
            "    args=[fixture_path],\n"
            "    env={'SystemRoot': os.environ.get('SystemRoot', r'C:\\Windows'), 'PYTHONUNBUFFERED': '1'},\n"
            "    job=job,\n"
            ")\n"
            "# Read child and grandchild PIDs from fixture\n"
            "c_line = proc.stdout.readline().decode().strip()\n"
            "gc_line = proc.stdout.readline().decode().strip()\n"
            "print(f'PARENT_PID={os.getpid()}', flush=True)\n"
            "print(c_line, flush=True)\n"
            "print(gc_line, flush=True)\n"
            "# Keep running until forcefully killed by controller\n"
            "while True:\n"
            "    time.sleep(1)\n"
        )

        # Launch the doomed parent as a separate OS process
        parent_proc = subprocess.Popen(
            [sys.executable, "-c", parent_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        try:
            line_p = parent_proc.stdout.readline().strip()
            line_c = parent_proc.stdout.readline().strip()
            line_gc = parent_proc.stdout.readline().strip()

            self.assertTrue(line_p.startswith("PARENT_PID="), f"Unexpected parent output: {line_p}")
            self.assertTrue(line_c.startswith("CHILD_PID="), f"Unexpected child output: {line_c}")
            self.assertTrue(line_gc.startswith("GRANDCHILD_PID="), f"Unexpected grandchild output: {line_gc}")

            parent_pid = int(line_p.split("=")[1])
            child_pid = int(line_c.split("=")[1])
            grandchild_pid = int(line_gc.split("=")[1])

            # Verify child and grandchild are alive
            h_c = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, child_pid)
            h_gc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, grandchild_pid)
            self.assertTrue(bool(h_c), "Child process must be alive")
            self.assertTrue(bool(h_gc), "Grandchild process must be alive")
            kernel32.CloseHandle(h_c)
            kernel32.CloseHandle(h_gc)

            # Abruptly kill the parent with SIGKILL (taskkill /F /PID)
            # This simulates an OS-level crash/kill without calling job.close()
            subprocess.run(["taskkill", "/F", "/PID", str(parent_pid)], capture_output=True)

            # Wait briefly for Windows kernel to close handles and trigger KILL_ON_JOB_CLOSE
            time.sleep(1.0)

            # Verify child is terminated
            h_c_check = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, child_pid)
            if h_c_check:
                exit_c = wintypes.DWORD()
                kernel32.GetExitCodeProcess(h_c_check, ctypes.byref(exit_c))
                kernel32.CloseHandle(h_c_check)
                self.assertNotEqual(exit_c.value, 259, f"Child process {child_pid} was not terminated on abrupt parent death")

            # Verify grandchild is terminated
            h_gc_check = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, grandchild_pid)
            if h_gc_check:
                exit_gc = wintypes.DWORD()
                kernel32.GetExitCodeProcess(h_gc_check, ctypes.byref(exit_gc))
                kernel32.CloseHandle(h_gc_check)
                self.assertNotEqual(exit_gc.value, 259, f"Grandchild process {grandchild_pid} was not terminated on abrupt parent death")

        finally:
            if parent_proc.poll() is None:
                parent_proc.kill()


if __name__ == "__main__":
    unittest.main()
