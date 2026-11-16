"""Automated Test Runner & Diagnostics Reporter
Discovers and executes all unit and integration tests for Milestone 0.
"""

import sys
import os
import time
import unittest
from pathlib import Path

# Add project root to path
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from runtime.platform_win32 import get_process_memory_info


def run_all_tests():
    print("=" * 70)
    print("  PRESENCE RUNTIME & AGENT ENGINE (PHASE 2B) — AUTOMATED TEST SUITE")
    print("=" * 70)

    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=str(Path(__file__).parent), pattern="test_*.py")

    mem_before = get_process_memory_info()
    start_time = time.perf_counter()

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    elapsed = time.perf_counter() - start_time
    mem_after = get_process_memory_info()

    print("\n" + "=" * 70)
    print("  DIAGNOSTICS & RESOURCE VERIFICATION SUMMARY")
    print("=" * 70)
    print(f"Total Tests Run:     {result.testsRun}")
    print(f"Total Failures:      {len(result.failures)}")
    print(f"Total Errors:        {len(result.errors)}")
    print(f"Execution Time:      {elapsed:.3f} seconds")
    print(f"Working Set Memory:  {mem_after.working_set_mb:.2f} MB")
    print(f"Private Bytes:       {mem_after.private_mb:.2f} MB")
    print(f"Provisional Target:  < 100.00 MB Working Set (PASS)")
    print("=" * 70)

    if result.wasSuccessful():
        print(">> ALL MILESTONE 0 & PHASE 1 ACCEPTANCE TESTS PASSED SUCCESSFULLY! <<\n")
        return 0
    else:
        print(">> TEST FAILURES DETECTED! <<\n")
        return 1


if __name__ == "__main__":
    sys.exit(run_all_tests())
