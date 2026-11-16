"""Automated Unit Tests for Win32 Job Object Subprocess Containment"""

import sys
import unittest
import subprocess
import time
from runtime.platform_win32 import Win32JobObject


class TestWin32JobObject(unittest.TestCase):
    """Validates Win32 Job Object creation, process assignment, and tree termination."""

    def test_job_object_lifecycle(self):
        job = Win32JobObject()
        success = job.create(kill_on_close=True)
        self.assertTrue(success)
        self.assertTrue(job.is_active)
        if sys.platform == "win32":
            self.assertIsNotNone(job.handle)
        job.close()
        self.assertFalse(job.is_active)
        self.assertIsNone(job.handle)

    def test_job_object_context_manager(self):
        with Win32JobObject() as job:
            self.assertTrue(job.is_active)
        self.assertFalse(job.is_active)

    def test_process_assignment_and_containment(self):
        if sys.platform != "win32":
            return

        with Win32JobObject() as job:
            # Spawn a long-running process (ping loop)
            proc = subprocess.Popen(
                ["cmd.exe", "/c", "ping 127.0.0.1 -n 10 > nul"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            assigned = job.assign_process(proc.pid)
            self.assertTrue(assigned)

            # Terminate job
            job.terminate(exit_code=42)
            proc.wait(timeout=3.0)
            if proc.stdout: proc.stdout.close()
            if proc.stderr: proc.stderr.close()
            self.assertIsNotNone(proc.returncode)

    def test_auto_kill_on_job_close(self):
        if sys.platform != "win32":
            return

        job = Win32JobObject()
        job.create(kill_on_close=True)

        proc = subprocess.Popen(
            ["cmd.exe", "/c", "ping 127.0.0.1 -n 10 > nul"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        assigned = job.assign_process(proc.pid)
        self.assertTrue(assigned)

        # Closing the job handle MUST automatically kill the assigned child process tree
        job.close()
        proc.wait(timeout=3.0)
        if proc.stdout: proc.stdout.close()
        if proc.stderr: proc.stderr.close()
        self.assertIsNotNone(proc.returncode)


if __name__ == "__main__":
    unittest.main()
