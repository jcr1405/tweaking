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
    print("  PRESENCE RUNTIME & AGENT ENGINE (PHASE 2B.1) — AUTOMATED TEST SUITE")
    print("=" * 70)

    # 1. Measure idle runtime memory baseline prior to test suite execution
    import runtime.main
    import runtime.mcp
    import runtime.agent_executor
    mem_idle = get_process_memory_info()
    idle_target = 100.0
    idle_pass = mem_idle.working_set_mb < idle_target
    idle_status = "PASS" if idle_pass else "FAIL"

    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=str(Path(__file__).parent), pattern="test_*.py")

    start_time = time.perf_counter()

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    elapsed = time.perf_counter() - start_time
    mem_after = get_process_memory_info()

    skipped_count = len(result.skipped) if hasattr(result, "skipped") else 0

    print("\n" + "=" * 70)
    print("  DIAGNOSTICS & RESOURCE VERIFICATION SUMMARY")
    print("=" * 70)
    print(f"Total Tests Run:           {result.testsRun}")
    print(f"Total Passed:             {result.testsRun - len(result.failures) - len(result.errors) - skipped_count}")
    print(f"Total Failures:           {len(result.failures)}")
    print(f"Total Errors:             {len(result.errors)}")
    print(f"Total Skipped:            {skipped_count}")
    print(f"Execution Time:           {elapsed:.3f} seconds")
    print(f"Idle Runtime Working Set: {mem_idle.working_set_mb:.2f} MB (Target: < {idle_target:.1f} MB -> {idle_status})")
    print(f"Idle Runtime Private:     {mem_idle.private_mb:.2f} MB")
    print(f"Test Suite Peak WS:       {mem_after.working_set_mb:.2f} MB (Cumulative 100+ tests execution)")
    print(f"Test Suite Private:       {mem_after.private_mb:.2f} MB")
    print("=" * 70)

    if result.wasSuccessful():
        print(">> ALL MILESTONE 0 & PHASE 1 ACCEPTANCE TESTS PASSED SUCCESSFULLY! <<\n")
        return 0
    else:
        print(">> TEST FAILURES DETECTED! <<\n")
        return 1


if __name__ == "__main__":
    sys.exit(run_all_tests())
