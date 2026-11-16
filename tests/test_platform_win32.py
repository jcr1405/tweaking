"""Unit Tests for Windows-Native Platform Integration
"""

import sys
import unittest
from runtime.platform_win32 import Win32SingleInstanceMutex, get_process_memory_info


class TestPlatformWin32(unittest.TestCase):

    def test_single_instance_mutex_acquisition_and_release(self):
        mutex_name = "Local\\TestPresenceMutex_Unique_001"
        m1 = Win32SingleInstanceMutex(mutex_name)
        acquired = m1.acquire()
        self.assertTrue(acquired, "First mutex acquisition must succeed")
        self.assertTrue(m1.is_owner)

        # Second acquisition on the same named mutex must fail
        m2 = Win32SingleInstanceMutex(mutex_name)
        acquired_second = m2.acquire()
        self.assertFalse(acquired_second, "Duplicate mutex acquisition must fail")
        self.assertFalse(m2.is_owner)

        # Release first mutex
        m1.release()

        # Now second mutex acquisition should succeed
        acquired_third = m2.acquire()
        self.assertTrue(acquired_third, "Acquisition after release must succeed")
        m2.release()

    def test_single_instance_context_manager(self):
        mutex_name = "Local\\TestPresenceMutex_Context_002"
        with Win32SingleInstanceMutex(mutex_name) as m:
            self.assertTrue(m.is_owner)
            # Inner attempt must fail
            m_inner = Win32SingleInstanceMutex(mutex_name)
            self.assertFalse(m_inner.acquire())

        # Once block exits, mutex is released; can be acquired again
        m_after = Win32SingleInstanceMutex(mutex_name)
        self.assertTrue(m_after.acquire())
        m_after.release()

    def test_process_memory_info(self):
        mem = get_process_memory_info()
        if sys.platform == "win32":
            self.assertGreater(mem.working_set_bytes, 0, "Working set must be positive on Windows")
            self.assertGreater(mem.working_set_mb, 1.0, "Working set should be at least 1MB")
            self.assertGreater(mem.peak_working_set_bytes, 0)
            print(f"\n[Test Diagnostics] Current Process Memory: {mem.working_set_mb:.2f} MB (Private: {mem.private_mb:.2f} MB)")


if __name__ == "__main__":
    unittest.main()
