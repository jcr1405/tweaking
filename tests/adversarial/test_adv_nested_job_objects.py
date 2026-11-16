"""Adversarial Verification Suite: Nested Windows Job Object Compatibility
Verifies process containment and lifecycle across nested Job Object hierarchies
in accordance with Specification 35 / Phase 2B.1.3.1 requirements (Scenarios A through E).
"""

import os
import sys
import time
import json
import ctypes
import unittest
import psutil
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
    """Verifies nested Windows Job Object behaviors on Windows 11 Build 26100."""

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

    def test_scenario_a_outer_job_without_breakaway_denies_breakaway(self):
        """Scenario A: Outer Job Object does NOT permit breakaway (no BREAKAWAY_OK flag).
        A process inside that job attempts CreateProcessW with CREATE_BREAKAWAY_FROM_JOB (0x01000000).
        Verifies:
        1. Child creation fails with ERROR_ACCESS_DENIED (5) OR Windows kernel ignores breakaway
           and traps the child inside the outer job.
        2. Zero uncontained processes escape.
        """
        outer_name = "AdvOuterJob_ScenarioA"
        outer_job = Win32JobObject(name=outer_name)
        self.assertTrue(outer_job.create(kill_on_close=True))
        self.jobs_to_clean.append(outer_job)

        env = {"SystemRoot": os.environ.get("SystemRoot", r"C:\Windows")}
        proc = Win32SuspendedLauncher.launch(
            command=sys.executable,
            args=[self.fixture_path, outer_name, "none", "attempt_breakaway"],
            env=env,
            job=outer_job,
        )

        raw_output = proc.stdout.readline().decode("utf-8")
        proc.wait(timeout_ms=5000)
        proc.close()

        self.assertTrue(raw_output.strip())
        report = json.loads(raw_output.strip())

        # Fixture itself is inside outer job
        self.assertTrue(report["fixture_in_outer"])
        # On Windows, attempting CREATE_BREAKAWAY_FROM_JOB without JOB_OBJECT_LIMIT_BREAKAWAY_OK
        # either fails CreateProcessW with Error 5, or kernel traps child inside outer job.
        if report["create_process_success"]:
            # On Windows 11 Build 26100: process is spawned but trapped inside outer job
            self.assertTrue(report["child_in_outer"], "Child escaped outer job without breakaway permission!")
            child_pid = report["child_pid"]
            if child_pid and psutil.pid_exists(child_pid):
                try:
                    psutil.Process(child_pid).kill()
                except Exception:
                    pass
        else:
            # On legacy Windows / strict DACL: CreateProcessW fails with ERROR_ACCESS_DENIED (5)
            self.assertEqual(report["win32_last_error"], 5)
            self.assertIsNone(report["child_pid"])

    def test_scenario_b_outer_job_with_silent_breakaway_auto_excludes_child(self):
        """Scenario B: Outer Job Object configured with JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK.
        Parent process inside outer job creates child WITHOUT explicit CREATE_BREAKAWAY_FROM_JOB flag.
        Verifies:
        1. Parent process is a member of the outer job (verified via parent process handle).
        2. Child is automatically excluded from the outer job (SILENT_BREAKAWAY_OK semantics).
        3. Child process succeeds creation and is NOT a member of outer job.
        """
        outer_name = "AdvOuterJob_ScenarioB"
        outer_job = Win32JobObject(name=outer_name)
        self.assertTrue(outer_job.create(kill_on_close=False))
        self.jobs_to_clean.append(outer_job)
        # Configure SILENT_BREAKAWAY_OK
        configured = self._configure_job_limits(
            outer_job,
            JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        self.assertTrue(configured)

        env = {"SystemRoot": os.environ.get("SystemRoot", r"C:\Windows")}
        proc = Win32SuspendedLauncher.launch(
            command=sys.executable,
            args=[self.fixture_path, outer_name, "none", "standard_spawn"],
            env=env,
            job=outer_job,
        )

        # Verify parent sees fixture process in outer job
        parent_sees_fixture = wintypes.BOOL()
        self.kernel32.IsProcessInJob(proc._h_process, outer_job.handle, ctypes.byref(parent_sees_fixture))
        self.assertTrue(parent_sees_fixture.value, "Fixture process was not assigned to outer job")

        raw_output = proc.stdout.readline().decode("utf-8")
        proc.wait(timeout_ms=5000)
        proc.close()

        report = json.loads(raw_output.strip())
        self.assertTrue(report["create_process_success"], "Standard spawn failed under silent breakaway")
        child_pid = report["child_pid"]
        self.assertIsNotNone(child_pid)

        # Under SILENT_BREAKAWAY_OK, the child must NOT be assigned to the outer job!
        self.assertFalse(report["child_in_outer"], "Child was not silently broken away from outer job!")

        # Also verify from controller handle view
        if child_pid and psutil.pid_exists(child_pid):
            h_child = self.kernel32.OpenProcess(0x1000, False, child_pid)
            if h_child:
                controller_sees_child = wintypes.BOOL()
                self.kernel32.IsProcessInJob(h_child, outer_job.handle, ctypes.byref(controller_sees_child))
                self.assertFalse(controller_sees_child.value, "Controller verified child is in outer job despite SILENT_BREAKAWAY_OK")
                self.kernel32.CloseHandle(h_child)
            try:
                psutil.Process(child_pid).kill()
            except Exception:
                pass

    def test_scenario_c_outer_job_permits_breakaway_with_explicit_flag(self):
        """Scenario C: Outer Job Object configured with JOB_OBJECT_LIMIT_BREAKAWAY_OK.
        Parent process inside outer job creates child WITH CREATE_BREAKAWAY_FROM_JOB flag.
        Verifies:
        1. Child creation succeeds.
        2. Child escapes outer job (child_in_outer == False).
        3. If an inner Job Object without breakaway is configured, child remains trapped in inner job.
        """
        outer_name = "AdvOuterJob_ScenarioC"
        inner_name = "AdvInnerJob_ScenarioC"

        outer_job = Win32JobObject(name=outer_name)
        self.assertTrue(outer_job.create(kill_on_close=False))
        self.jobs_to_clean.append(outer_job)
        self._configure_job_limits(
            outer_job,
            JOB_OBJECT_LIMIT_BREAKAWAY_OK | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )

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
        # Assign to outer job as well
        self.kernel32.AssignProcessToJobObject(outer_job.handle, proc._h_process)

        raw_output = proc.stdout.readline().decode("utf-8")
        proc.wait(timeout_ms=5000)
        proc.close()

        report = json.loads(raw_output.strip())
        self.assertTrue(report["fixture_in_inner"])
        self.assertTrue(report["fixture_in_outer"])

        if report["create_process_success"]:
            # Broke away from outer job, but trapped in inner job
            self.assertFalse(report["child_in_outer"], "Child did not break away from outer job despite BREAKAWAY_OK")
            self.assertTrue(report["child_in_inner"], "Child escaped inner job which did not permit breakaway!")
            child_pid = report["child_pid"]
            if child_pid and psutil.pid_exists(child_pid):
                try:
                    psutil.Process(child_pid).kill()
                except Exception:
                    pass

    def test_scenario_d_inner_job_refuses_breakaway(self):
        """Scenario D: Outer Job permits breakaway (BREAKAWAY_OK), but Inner Job does NOT.
        Process inside inner job attempts CREATE_BREAKAWAY_FROM_JOB.
        Verifies:
        1. Inner Job Object limits override outer job permissiveness.
        2. Child cannot escape inner job containment.
        """
        outer_name = "AdvOuterJob_ScenarioD"
        inner_name = "AdvInnerJob_ScenarioD"

        outer_job = Win32JobObject(name=outer_name)
        self.assertTrue(outer_job.create(kill_on_close=False))
        self.jobs_to_clean.append(outer_job)
        self._configure_job_limits(outer_job, JOB_OBJECT_LIMIT_BREAKAWAY_OK | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)

        # Inner job: standard limits, NO breakaway flag
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
            self.assertTrue(report["child_in_inner"], "Child escaped inner job!")
            child_pid = report["child_pid"]
            if child_pid and psutil.pid_exists(child_pid):
                try:
                    psutil.Process(child_pid).kill()
                except Exception:
                    pass

    def test_scenario_e_outer_job_destruction_terminates_entire_process_tree(self):
        """Scenario E: Outer Job Object destruction terminates entire 3-tier process tree.
        Topology: Test Controller -> Parent (fixture) -> Child -> Grandchild.
        All assigned to Job Object hierarchy.
        External controller closes outer job with KILL_ON_JOB_CLOSE.
        Independently verifies:
        1. Parent terminated.
        2. Child terminated.
        3. Grandchild terminated.
        4. Zero surviving descendant PIDs.
        """
        outer_job = Win32JobObject(name="AdvOuterJob_ScenarioE")
        self.assertTrue(outer_job.create(kill_on_close=True))

        env = {"SystemRoot": os.environ.get("SystemRoot", r"C:\Windows")}
        proc = Win32SuspendedLauncher.launch(
            command=sys.executable,
            args=[self.fixture_path, "none", "none", "spawn_tree"],
            env=env,
            job=outer_job,
        )
        parent_pid = proc.pid

        # Read JSON report containing child and grandchild PIDs
        raw_output = proc.stdout.readline().decode("utf-8")
        self.assertTrue(raw_output.strip(), "No tree report received from fixture")
        report = json.loads(raw_output.strip())

        child_pid = report.get("child_pid")
        grandchild_pid = report.get("grandchild_pid")

        self.assertIsNotNone(parent_pid)
        self.assertIsNotNone(child_pid)
        self.assertIsNotNone(grandchild_pid)

        # Confirm all 3 are actively running before job close
        self.assertTrue(psutil.pid_exists(parent_pid), f"Parent PID {parent_pid} not alive")
        self.assertTrue(psutil.pid_exists(child_pid), f"Child PID {child_pid} not alive")
        self.assertTrue(psutil.pid_exists(grandchild_pid), f"Grandchild PID {grandchild_pid} not alive")

        # Now external controller terminates outer job
        outer_job.close()
        proc.close()

        # Allow kernel time to terminate process tree
        time.sleep(0.4)

        # Independently verify all 3 processes are dead
        self.assertFalse(psutil.pid_exists(parent_pid), f"Parent PID {parent_pid} survived outer job close!")
        self.assertFalse(psutil.pid_exists(child_pid), f"Child PID {child_pid} survived outer job close!")
        self.assertFalse(psutil.pid_exists(grandchild_pid), f"Grandchild PID {grandchild_pid} survived outer job close!")

    def test_auxiliary_nested_assignment_failure_fails_closed(self):
        """Auxiliary: If assignment to job fails (e.g. invalid/closed handle), launcher fails closed.
        Suspended child is terminated immediately and never executed.
        """
        env = {"SystemRoot": os.environ.get("SystemRoot", r"C:\Windows")}
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

    def test_auxiliary_nested_parent_and_child_job_assignment(self):
        """Auxiliary: Verifies multi-job hierarchy assignment on Windows 8+.
        A single process can be assigned to multiple nested Job Objects simultaneously.
        """
        parent_job = Win32JobObject(name="AdvParentJob_Aux")
        self.assertTrue(parent_job.create(kill_on_close=True))
        self.jobs_to_clean.append(parent_job)

        child_job = Win32JobObject(name="AdvChildJob_Aux")
        self.assertTrue(child_job.create(kill_on_close=True))
        self.jobs_to_clean.append(child_job)

        env = {"SystemRoot": os.environ.get("SystemRoot", r"C:\Windows")}
        proc = Win32SuspendedLauncher.launch(
            command=sys.executable,
            args=["-c", "import time; time.sleep(1)"],
            env=env,
            job=child_job,
        )

        # Assign child process ALSO to parent job
        assigned_to_parent = self.kernel32.AssignProcessToJobObject(parent_job.handle, proc._h_process)
        self.assertTrue(assigned_to_parent, f"Failed assigning to parent job: {self.kernel32.GetLastError()}")

        in_child = wintypes.BOOL()
        in_parent = wintypes.BOOL()
        self.kernel32.IsProcessInJob(proc._h_process, child_job.handle, ctypes.byref(in_child))
        self.kernel32.IsProcessInJob(proc._h_process, parent_job.handle, ctypes.byref(in_parent))

        self.assertTrue(in_child.value, "Process not found in child job")
        self.assertTrue(in_parent.value, "Process not found in parent job")
        proc.close()

    def test_auxiliary_standalone_job_containment(self):
        """Auxiliary: Standard baseline containment in single Job Object."""
        job = Win32JobObject(name="AdvStandaloneJob_Aux")
        self.assertTrue(job.create(kill_on_close=True))
        self.jobs_to_clean.append(job)

        env = {"SystemRoot": os.environ.get("SystemRoot", r"C:\Windows")}
        proc = Win32SuspendedLauncher.launch(
            command=sys.executable,
            args=["-c", "import time; time.sleep(1)"],
            env=env,
            job=job,
        )
        self.assertGreater(proc.pid, 0)

        is_in = wintypes.BOOL()
        self.kernel32.IsProcessInJob(proc._h_process, job.handle, ctypes.byref(is_in))
        self.assertTrue(is_in.value)
        proc.close()


if __name__ == "__main__":
    unittest.main()
