"""Adversarial Verification Suite: Nested Windows Job Object Compatibility
Verifies process containment and lifecycle across nested Job Object hierarchies.
"""

import os
import sys
import time
import json
import ctypes
import unittest
from pathlib import Path
from ctypes import wintypes
from runtime.platform_win32 import (
    Win32JobObject,
    Win32SuspendedLauncher,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    JobObjectExtendedLimitInformation,
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
)

JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000


class TestAdvNestedJobObjects(unittest.TestCase):
    """Verifies nested Windows Job Object behaviors on Windows 11."""

    def setUp(self):
        self.kernel32 = ctypes.windll.kernel32
        self.jobs_to_clean = []
        self.fixture_path = str(Path(__file__).parent.parent / "fixtures" / "job_breakaway_fixture.py")
        self.assertTrue(os.path.exists(self.fixture_path))

    def tearDown(self):
        for job in self.jobs_to_clean:
            try:
                job.close()
            except Exception:
                pass

    def _configure_job_limits(self, job: Win32JobObject, limit_flags: int) -> bool:
        """Helper to configure specific JOBOBJECT_EXTENDED_LIMIT_INFORMATION flags."""
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = limit_flags
        return bool(self.kernel32.SetInformationJobObject(
            job.handle,
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ))

    def test_scenario_a_ordinary_outer_job_descendant_breakaway(self):
        """Scenario A: Ordinary outer Job Object without breakaway permission.
        Child attempts CREATE_BREAKAWAY_FROM_JOB.
        Verifies that Windows 11 kernel traps child inside inner Job Object anyway.
        """
        outer_name = "AdvOuterJob_ScenarioA"
        inner_name = "AdvInnerJob_ScenarioA"

        outer_job = Win32JobObject(name=outer_name)
        self.assertTrue(outer_job.create(kill_on_close=True))
        self.jobs_to_clean.append(outer_job)

        inner_job = Win32JobObject(name=inner_name)
        self.assertTrue(inner_job.create(kill_on_close=True))
        self.jobs_to_clean.append(inner_job)

        env = {"SystemRoot": os.environ.get("SystemRoot", r"C:\Windows")}
        proc = Win32SuspendedLauncher.launch(
            command=sys.executable,
            args=[self.fixture_path, outer_name, inner_name, "attempt_breakaway"],
            env=env,
            job=inner_job,
        )

        # Also assign process to outer job
        self.kernel32.AssignProcessToJobObject(outer_job.handle, proc._h_process)

        # Read JSON report from fixture stdout
        raw_output = proc.stdout.readline().decode("utf-8")
        proc.wait(timeout_ms=5000)
        proc.close()

        self.assertTrue(raw_output.strip())
        report = json.loads(raw_output.strip())

        # Assertions
        self.assertTrue(report["fixture_in_inner"])
        self.assertTrue(report["fixture_in_outer"])
        # Even with CREATE_BREAKAWAY_FROM_JOB requested:
        if report["create_process_success"]:
            # On Windows 11, grandchild is trapped inside inner job
            self.assertTrue(report["child_in_inner"], "Grandchild escaped inner Job Object!")
        else:
            # Or denied with ERROR_ACCESS_DENIED (5)
            self.assertEqual(report["win32_last_error"], 5)

    def test_scenario_b_outer_job_with_silent_breakaway(self):
        """Scenario B: Outer Job Object configured with JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK.
        Verifies that inner Job Object containment remains authoritative for descendants.
        """
        outer_name = "AdvOuterJob_ScenarioB"
        inner_name = "AdvInnerJob_ScenarioB"

        outer_job = Win32JobObject(name=outer_name)
        outer_job.create(kill_on_close=False)
        self.jobs_to_clean.append(outer_job)
        self._configure_job_limits(outer_job, JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)

        inner_job = Win32JobObject(name=inner_name)
        self.assertTrue(inner_job.create(kill_on_close=True))
        self.jobs_to_clean.append(inner_job)

        env = {"SystemRoot": os.environ.get("SystemRoot", r"C:\Windows")}
        proc = Win32SuspendedLauncher.launch(
            command=sys.executable,
            args=[self.fixture_path, outer_name, inner_name, "attempt_breakaway"],
            env=env,
            job=inner_job,
        )
        self.kernel32.AssignProcessToJobObject(outer_job.handle, proc._h_process)

        raw_output = proc.stdout.readline().decode("utf-8")
        proc.wait(timeout_ms=5000)
        proc.close()

        report = json.loads(raw_output.strip())
        self.assertTrue(report["fixture_in_inner"])
        if report["create_process_success"]:
            self.assertTrue(report["child_in_inner"], "Descendant escaped inner Job Object under silent breakaway!")

    def test_scenario_c_outer_job_permits_breakaway(self):
        """Scenario C: Outer Job Object explicitly permits breakaway (JOB_OBJECT_LIMIT_BREAKAWAY_OK).
        Verifies that inner Job Object containment still traps the child process tree.
        """
        outer_name = "AdvOuterJob_ScenarioC"
        inner_name = "AdvInnerJob_ScenarioC"

        outer_job = Win32JobObject(name=outer_name)
        outer_job.create(kill_on_close=False)
        self.jobs_to_clean.append(outer_job)
        self._configure_job_limits(outer_job, JOB_OBJECT_LIMIT_BREAKAWAY_OK | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)

        inner_job = Win32JobObject(name=inner_name)
        self.assertTrue(inner_job.create(kill_on_close=True))
        self.jobs_to_clean.append(inner_job)

        env = {"SystemRoot": os.environ.get("SystemRoot", r"C:\Windows")}
        proc = Win32SuspendedLauncher.launch(
            command=sys.executable,
            args=[self.fixture_path, outer_name, inner_name, "attempt_breakaway"],
            env=env,
            job=inner_job,
        )
        self.kernel32.AssignProcessToJobObject(outer_job.handle, proc._h_process)

        raw_output = proc.stdout.readline().decode("utf-8")
        proc.wait(timeout_ms=5000)
        proc.close()

        report = json.loads(raw_output.strip())
        self.assertTrue(report["fixture_in_inner"])
        if report["create_process_success"]:
            # Even if it broke away from outer job, it must remain inside inner job
            self.assertTrue(report["child_in_inner"], "Descendant escaped inner Job Object!")

    def test_scenario_d_inner_job_refuses_breakaway(self):
        """Scenario D: Inner Job Object does not configure BREAKAWAY_OK.
        Verifies that descendants cannot escape the inner job.
        """
        inner_name = "AdvInnerJob_ScenarioD"
        inner_job = Win32JobObject(name=inner_name)
        self.assertTrue(inner_job.create(kill_on_close=True))
        self.jobs_to_clean.append(inner_job)

        env = {"SystemRoot": os.environ.get("SystemRoot", r"C:\Windows")}
        proc = Win32SuspendedLauncher.launch(
            command=sys.executable,
            args=[self.fixture_path, "none", inner_name, "attempt_breakaway"],
            env=env,
            job=inner_job,
        )

        raw_output = proc.stdout.readline().decode("utf-8")
        proc.wait(timeout_ms=5000)
        proc.close()

        report = json.loads(raw_output.strip())
        self.assertTrue(report["fixture_in_inner"])
        if report["create_process_success"]:
            self.assertTrue(report["child_in_inner"])

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
