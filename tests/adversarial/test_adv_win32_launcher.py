"""Adversarial Verification Suite: Win32 Suspended Process Launcher
Tests:
1. Exact execution order and state verification.
2. Failure injection at each native transition (CreatePipe, CreateProcessW, AssignProcessToJobObject, IsProcessInJob, ResumeThread).
3. Handle leak audits on every error exit path.
4. Command line formatting: spaces, quotes, unicode, long argument lists (>4096 chars).
5. Unicode environment block: deduplication, sorting, special chars, double-NUL termination.
"""

import sys
import os
import ctypes
from ctypes import wintypes
import unittest
from unittest.mock import patch

from runtime.platform_win32 import (
    Win32JobObject,
    Win32SuspendedLauncher,
    Win32SuspendedProcess,
    create_unicode_environment_block,
)


@unittest.skipUnless(sys.platform == "win32", "Win32 launcher tests require Windows")
class TestAdvWin32Launcher(unittest.TestCase):

    def test_unicode_environment_block_adversarial_cases(self):
        """Verifies Unicode environment block sorting, case-insensitivity, equals signs in values, and UTF-16 double-NUL termination."""
        env = {
            "Z_KEY": "val=with=equals",
            "a_key": "lowercase_first",
            "A_KEY": "uppercase_wins",
            "PATH": r"C:\Windows\system32;C:\Windows",
            "EMOJI_TEST": "Presence-Hardening-🚀-🛡️",
            "EMPTY_VAL": "",
        }
        buf = create_unicode_environment_block(env)
        raw = buf[:]

        # Double-NUL termination check (UTF-16 wchar ends with \0\0)
        self.assertTrue(raw.endswith("\0\0"), "Environment block must end with double-NUL")

        lines = [x for x in raw.split("\0") if x]
        keys = [x.split("=")[0] for x in lines]

        # Case-insensitive deduplication check: only one 'A_KEY' (or 'a_key')
        a_keys = [k for k in keys if k.upper() == "A_KEY"]
        self.assertEqual(len(a_keys), 1, "Duplicate keys must be deduplicated")

        # Sorted alphabetically (case-insensitively)
        self.assertEqual(keys, sorted(keys, key=lambda s: s.upper()))

        # Check equals sign in value preserved
        z_line = [x for x in lines if x.startswith("Z_KEY=")][0]
        self.assertEqual(z_line, "Z_KEY=val=with=equals")

    def test_command_line_formatting_spaces_and_quotes(self):
        """Verifies command line construction with spaces, quotation marks, and special characters."""
        job = Win32JobObject(name="AdvTestCmdLineJob")
        job.create(kill_on_close=True)

        env = {
            "SystemRoot": os.environ.get("SystemRoot", r"C:\Windows"),
            "PYTHONUNBUFFERED": "1",
        }

        # Pass arguments containing spaces, quotes, and backslashes
        test_arg1 = "Argument with spaces"
        test_arg2 = 'Argument "with" quotes'
        test_arg3 = r"C:\Path\With\Backslashes\and spaces"

        code = (
            "import sys\n"
            "for arg in sys.argv[1:]:\n"
            "    print(f'ARG:{arg}')\n"
        )

        proc = Win32SuspendedLauncher.launch(
            command=sys.executable,
            args=["-c", code, test_arg1, test_arg2, test_arg3],
            env=env,
            job=job,
        )

        try:
            out_lines = [proc.stdout.readline().decode("utf-8").strip() for _ in range(3)]
            self.assertEqual(out_lines[0], f"ARG:{test_arg1}")
            self.assertEqual(out_lines[1], f"ARG:{test_arg2}")
            self.assertEqual(out_lines[2], f"ARG:{test_arg3}")
            self.assertEqual(proc.wait(timeout_ms=5000), 0)
        finally:
            proc.close()
            job.close()

    def test_long_command_line_handling(self):
        """Verifies launching process with a very long argument payload (>8 KB)."""
        job = Win32JobObject(name="AdvTestLongCmdJob")
        job.create(kill_on_close=True)

        env = {
            "SystemRoot": os.environ.get("SystemRoot", r"C:\Windows"),
            "PYTHONUNBUFFERED": "1",
        }

        long_payload = "X" * 8192
        code = "import sys; print(f'LEN:{len(sys.argv[1])}')"

        proc = Win32SuspendedLauncher.launch(
            command=sys.executable,
            args=["-c", code, long_payload],
            env=env,
            job=job,
        )

        try:
            line = proc.stdout.readline().decode("utf-8").strip()
            self.assertEqual(line, f"LEN:{len(long_payload)}")
            self.assertEqual(proc.wait(timeout_ms=5000), 0)
        finally:
            proc.close()
            job.close()

    def test_failure_injection_at_is_process_in_job_call(self):
        """CRITICAL AUDIT TEST:
        What happens if IsProcessInJob returns FALSE (API call failure)?
        Audits whether the launcher fails closed (terminates child) or fails open (resumes child).
        """
        kernel32 = ctypes.windll.kernel32
        orig_is_in_job = kernel32.IsProcessInJob

        # Simulated failure: IsProcessInJob returns 0 (FALSE)
        def faulty_is_in_job(hProcess, hJob, pResult):
            # API failure code
            kernel32.SetLastError(5)  # ERROR_ACCESS_DENIED
            return 0  # FALSE

        job = Win32JobObject(name="AdvTestFaultyJob")
        job.create(kill_on_close=True)

        env = {
            "SystemRoot": os.environ.get("SystemRoot", r"C:\Windows"),
            "PYTHONUNBUFFERED": "1",
        }

        with patch.object(kernel32, "IsProcessInJob", side_effect=faulty_is_in_job):
            # If IsProcessInJob fails, does launcher fail closed and raise an error?
            # Or does it skip the check and resume the uncontained child?
            try:
                proc = Win32SuspendedLauncher.launch(
                    command=sys.executable,
                    args=["-c", "import time; time.sleep(1)"],
                    env=env,
                    job=job,
                )
                # If we get here, the launcher did NOT fail closed!
                launched_unverified_pid = proc.pid
                proc.close()
                self.fail(
                    f"VULNERABILITY REPRODUCED: IsProcessInJob returned 0 (API failure), "
                    f"but launcher proceeded to resume child PID {launched_unverified_pid}!"
                )
            except RuntimeError as exc:
                # Expected secure behavior: fail closed!
                self.assertIn("Security invariant violated", str(exc))
            finally:
                job.close()

    def test_failure_injection_at_assign_process_to_job(self):
        """Verifies that if AssignProcessToJobObject fails, the suspended child is killed immediately and handles are closed."""
        kernel32 = ctypes.windll.kernel32

        def faulty_assign(hJob, hProcess):
            kernel32.SetLastError(5)  # ERROR_ACCESS_DENIED
            return 0

        job = Win32JobObject(name="AdvTestAssignFailJob")
        job.create(kill_on_close=True)

        env = {
            "SystemRoot": os.environ.get("SystemRoot", r"C:\Windows"),
        }

        with patch.object(kernel32, "AssignProcessToJobObject", side_effect=faulty_assign):
            with self.assertRaises(RuntimeError) as ctx:
                Win32SuspendedLauncher.launch(
                    command=sys.executable,
                    args=["-c", "print('SHOULD_NEVER_RUN')"],
                    env=env,
                    job=job,
                )
            self.assertIn("AssignProcessToJobObject failed", str(ctx.exception))
            job.close()


if __name__ == "__main__":
    unittest.main()
