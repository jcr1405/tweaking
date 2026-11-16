"""Windows-Native Platform Integration
Direct Win32 API interoperability using ctypes for named mutexes,
process memory metrics, and OS console control events.
"""

import sys
import os
import ctypes
from ctypes import wintypes
from typing import Optional, NamedTuple, Callable

# Constants
ERROR_ALREADY_EXISTS = 183
CTRL_C_EVENT = 0
CTRL_BREAK_EVENT = 1
CTRL_CLOSE_EVENT = 2
CTRL_LOGOFF_EVENT = 5
CTRL_SHUTDOWN_EVENT = 6


class ProcessMemoryInfo(NamedTuple):
    working_set_bytes: int
    private_bytes: int
    peak_working_set_bytes: int

    @property
    def working_set_mb(self) -> float:
        return self.working_set_bytes / (1024 * 1024)

    @property
    def private_mb(self) -> float:
        return self.private_bytes / (1024 * 1024)


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


class Win32SingleInstanceMutex:
    """Enforces single-instance execution via a Windows Named Mutex.
    Guarantees duplicate instances exit immediately without race conditions.
    """

    def __init__(self, mutex_name: str = "Local\\PresenceRuntime_SingleInstanceMutex"):
        self.mutex_name = mutex_name
        self._handle: Optional[wintypes.HANDLE] = None
        self._is_owner: bool = False

    def acquire(self) -> bool:
        """Attempts to create and acquire ownership of the named mutex.
        Returns True if this is the sole instance, False if an instance already exists.
        """
        if sys.platform != "win32":
            # Non-Windows fallback (for cross-platform linting/testing)
            self._is_owner = True
            return True

        kernel32 = ctypes.windll.kernel32
        kernel32.SetLastError(0)

        # CreateMutexW(lpMutexAttributes, bInitialOwner, lpName)
        self._handle = kernel32.CreateMutexW(None, True, self.mutex_name)
        last_error = kernel32.GetLastError()

        if not self._handle:
            return False

        if last_error == ERROR_ALREADY_EXISTS:
            self._is_owner = False
            # Close handle to avoid leaking reference
            kernel32.CloseHandle(self._handle)
            self._handle = None
            return False

        self._is_owner = True
        return True

    def release(self) -> None:
        """Releases and closes the mutex handle."""
        if sys.platform == "win32" and self._handle:
            kernel32 = ctypes.windll.kernel32
            if self._is_owner:
                kernel32.ReleaseMutex(self._handle)
            kernel32.CloseHandle(self._handle)
            self._handle = None
            self._is_owner = False

    def __enter__(self):
        acquired = self.acquire()
        if not acquired:
            raise RuntimeError(f"Duplicate instance detected: {self.mutex_name} already exists.")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()

    @property
    def is_owner(self) -> bool:
        return self._is_owner


def get_process_memory_info(pid: Optional[int] = None) -> ProcessMemoryInfo:
    """Retrieves physical Working Set and Private memory bytes for the specified PID (or current process)."""
    if sys.platform != "win32":
        return ProcessMemoryInfo(0, 0, 0)

    kernel32 = ctypes.windll.kernel32
    # Explicitly configure 64-bit argtypes and restype to prevent handle truncation
    kernel32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    kernel32.K32GetProcessMemoryInfo.restype = wintypes.BOOL

    if pid is None or pid == os.getpid():
        h_process = kernel32.GetCurrentProcess()
        should_close = False
    else:
        PROCESS_QUERY_INFORMATION = 0x0400
        PROCESS_VM_READ = 0x0010
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        h_process = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not h_process:
            return ProcessMemoryInfo(0, 0, 0)
        should_close = True

    try:
        counters = PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS_EX)

        if kernel32.K32GetProcessMemoryInfo(
            h_process,
            ctypes.byref(counters),
            counters.cb,
        ):
            return ProcessMemoryInfo(
                working_set_bytes=counters.WorkingSetSize,
                private_bytes=counters.PrivateUsage,
                peak_working_set_bytes=counters.PeakWorkingSetSize,
            )
        return ProcessMemoryInfo(0, 0, 0)
    finally:
        if should_close and h_process:
            kernel32.CloseHandle(h_process)


# Global reference to prevent callback garbage collection
_ctrl_handler_ref = None

def register_console_ctrl_handler(callback: Callable[[int], bool]) -> bool:
    """Registers a Win32 Console Control Handler for clean OS shutdown/Ctrl+C signals."""
    global _ctrl_handler_ref
    if sys.platform != "win32":
        return False

    kernel32 = ctypes.windll.kernel32
    PHANDLER_ROUTINE = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

    def _native_handler(ctrl_type):
        return wintypes.BOOL(callback(ctrl_type))

    _ctrl_handler_ref = PHANDLER_ROUTINE(_native_handler)
    return bool(kernel32.SetConsoleCtrlHandler(_ctrl_handler_ref, True))


# Job Object Constants
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JobObjectExtendedLimitInformation = 9
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryLimit", ctypes.c_size_t),
        ("PeakJobMemoryLimit", ctypes.c_size_t),
    ]


class Win32JobObject:
    """Windows Job Object container for robust subprocess containment.
    Enforces JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE so child process trees
    are automatically terminated by the Windows kernel when the job is closed.
    """

    def __init__(self, name: Optional[str] = None):
        self.name = name
        self._handle: Optional[wintypes.HANDLE] = None
        self._is_active: bool = False

    @property
    def handle(self) -> Optional[wintypes.HANDLE]:
        return self._handle

    @property
    def is_active(self) -> bool:
        return self._is_active

    def create(self, kill_on_close: bool = True) -> bool:
        """Creates the Job Object and configures extended limit information."""
        if sys.platform != "win32":
            self._is_active = True
            return True

        kernel32 = ctypes.windll.kernel32
        self._handle = kernel32.CreateJobObjectW(None, self.name)
        if not self._handle:
            return False

        if kill_on_close:
            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            success = kernel32.SetInformationJobObject(
                self._handle,
                JobObjectExtendedLimitInformation,
                ctypes.byref(info),
                ctypes.sizeof(info)
            )
            if not success:
                kernel32.CloseHandle(self._handle)
                self._handle = None
                return False

        self._is_active = True
        return True

    def assign_process(self, pid: int) -> bool:
        """Assigns an OS process to this Job Object by its PID."""
        if sys.platform != "win32":
            return True

        if not self._handle or not self._is_active:
            return False

        kernel32 = ctypes.windll.kernel32
        h_process = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, wintypes.DWORD(pid))
        if not h_process:
            return False

        try:
            return bool(kernel32.AssignProcessToJobObject(self._handle, h_process))
        finally:
            kernel32.CloseHandle(h_process)

    def terminate(self, exit_code: int = 1) -> bool:
        """Terminates all processes currently assigned to the job object."""
        if sys.platform != "win32":
            return True

        if not self._handle:
            return False

        kernel32 = ctypes.windll.kernel32
        return bool(kernel32.TerminateJobObject(self._handle, wintypes.UINT(exit_code)))

    def close(self) -> None:
        """Closes the Job Object handle, triggering kernel-level tree termination if KILL_ON_JOB_CLOSE was set."""
        if sys.platform == "win32" and self._handle:
            kernel32 = ctypes.windll.kernel32
            kernel32.CloseHandle(self._handle)
            self._handle = None
            self._is_active = False

    def __enter__(self):
        self.create()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

