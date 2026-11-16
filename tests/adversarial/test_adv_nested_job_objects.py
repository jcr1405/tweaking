"""Adversarial Verification Suite: Nested Windows Job Object Compatibility
Verifies process containment and lifecycle across nested Job Object hierarchies.
"""

import os
import sys
import time
import ctypes
import unittest
from ctypes import wintypes
from runtime.platform_win32 import (
    Win32JobObject,
    Win32SuspendedLauncher,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    JobObjectExtendedLimitInformation,
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
)


class TestAdvNestedJobObjects(unittest.TestCase):
    """Verifies nested Windows Job Object behaviors on Windows 11."""

    def setUp(self):
        self.kernel32 = ctypes.windll.kernel32
        self.jobs_to_clean = []

    def tearDown(self):
        for job in self.jobs_to_clean:
            try:
                job.close()
            except Exception:
                pass

    def test_scenario_a_no_parent_job(self):
        """Scenario A: Standard execution without existing parent job."""
        child_job = Win32JobObject(name="AdvNestedJob_ScenarioA")
        self.assertTrue(child_job.create(kill_on_close=True))
        self.jobs_to_clean.append(child_job)

        env = {"SystemRoot": os.environ.get("SystemRoot", r"C:\Windows")}
        proc = Win32SuspendedLauncher.launch(
            command=sys.executable,
            args=["-c", "import time; time.sleep(1)"],
            env=env,
            job=child_job,
        )
        self.assertGreater(proc.pid, 0)

        # Verify membership
        is_in_job = wintypes.BOOL()
        self.kernel32.IsProcessInJob(proc._h_process, child_job.handle, ctypes.byref(is_in_job))
        self.assertTrue(is_in_job.value)
        proc.close()

    def test_scenario_b_nested_parent_and_child_jobs(self):
        """Scenario B: Parent Job Object permits nested jobs.
        An outer parent job is created. A subprocess is assigned to parent job.
        That subprocess (or launcher) creates a child job and assigns a child process.
        """
        parent_job = Win32JobObject(name="AdvParentJob_ScenarioB")
        self.assertTrue(parent_job.create(kill_on_close=True))
        self.jobs_to_clean.append(parent_job)

        child_job = Win32JobObject(name="AdvChildJob_ScenarioB")
        self.assertTrue(child_job.create(kill_on_close=True))
        self.jobs_to_clean.append(child_job)

        env = {"SystemRoot": os.environ.get("SystemRoot", r"C:\Windows")}
        proc = Win32SuspendedLauncher.launch(
            command=sys.executable,
            args=["-c", "import time; time.sleep(1)"],
            env=env,
            job=child_job,
        )
        self.assertGreater(proc.pid, 0)

        # Assign child process ALSO to parent job (creating a nested hierarchy)
        # In Windows 8+, a process can belong to multiple jobs in a hierarchy
        assigned_to_parent = self.kernel32.AssignProcessToJobObject(parent_job.handle, proc._h_process)
        self.assertTrue(assigned_to_parent, f"Failed assigning to parent job: {self.kernel32.GetLastError()}")

        # Verify child is member of BOTH jobs
        in_child = wintypes.BOOL()
        in_parent = wintypes.BOOL()
        self.kernel32.IsProcessInJob(proc._h_process, child_job.handle, ctypes.byref(in_child))
        self.kernel32.IsProcessInJob(proc._h_process, parent_job.handle, ctypes.byref(in_parent))

        self.assertTrue(in_child.value, "Process not found in child job")
        self.assertTrue(in_parent.value, "Process not found in parent job")
        proc.close()

    def test_scenario_c_nested_assignment_failure_fails_closed(self):
        """Scenario C: If assignment to child job fails (simulated), launcher must fail closed.
        Suspended child is terminated immediately and never executed.
        """
        env = {"SystemRoot": os.environ.get("SystemRoot", r"C:\Windows")}
        # Create an invalid/closed job object handle to trigger failure
        bad_job = Win32JobObject(name="AdvInvalidJob")
        bad_job._handle = wintypes.HANDLE(0xDEADBEEF)
        bad_job._is_active = True

        with self.assertRaises(RuntimeError) as ctx:
            Win32SuspendedLauncher.launch(
                command=sys.executable,
                args=["-c", "import time; time.sleep(1)"],
                env=env,
                job=bad_job,
            )
        self.assertIn("AssignProcessToJobObject failed", str(ctx.exception))

    def test_scenario_e_abrupt_outer_parent_termination_cleans_hierarchy(self):
        """Scenario E: Outer parent job closure terminates the entire process tree."""
        import subprocess

        parent_job = Win32JobObject(name="AdvParentJob_ScenarioE")
        self.assertTrue(parent_job.create(kill_on_close=True))

        env = {"SystemRoot": os.environ.get("SystemRoot", r"C:\Windows")}
        child_job = Win32JobObject(name="AdvChildJob_ScenarioE")
        self.assertTrue(child_job.create(kill_on_close=True))
        self.jobs_to_clean.append(child_job)

        proc = Win32SuspendedLauncher.launch(
            command=sys.executable,
            args=["-c", "import time; time.sleep(10)"],
            env=env,
            job=child_job,
        )
        child_pid = proc.pid

        # Also assign to parent job
        self.kernel32.AssignProcessToJobObject(parent_job.handle, proc._h_process)

        # Abruptly close parent job handle
        parent_job.close()

        # Check that child process is dead within 1.0 second
        time.sleep(0.3)
        check = subprocess.run(["tasklist", "/FI", f"PID eq {child_pid}"], capture_output=True, text=True)
        self.assertNotIn(str(child_pid), check.stdout)


if __name__ == "__main__":
    unittest.main()
