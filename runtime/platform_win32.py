"""Windows-Native Platform Integration
Direct Win32 API interoperability using ctypes for named mutexes,
process memory metrics, and OS console control events.
"""

import sys
import os
import time
import ctypes
from ctypes import wintypes
from typing import Optional, NamedTuple, Callable, Dict, Tuple, List

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


# Windows Job Object and Process Creation Constants
STARTF_USESTDHANDLES = 0x00000100
CREATE_SUSPENDED = 0x00000004
CREATE_NO_WINDOW = 0x08000000
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
HANDLE_FLAG_INHERIT = 0x00000001
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
WAIT_TIMEOUT = 0x00000102
STILL_ACTIVE = 259
INFINITE = 0xFFFFFFFF


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [
        ("StartupInfo", STARTUPINFOW),
        ("lpAttributeList", ctypes.c_void_p),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


def create_unicode_environment_block(env_dict: Dict[str, str]) -> ctypes.Array:
    """Builds a sorted, case-insensitive deduplicated, double-NUL terminated Unicode environment block.
    Windows API expects environment blocks sorted alphabetically by variable name (case-insensitively).
    """
    # Case-insensitive deduplication, keeping later definitions
    normalized: Dict[str, Tuple[str, str]] = {}
    for k, v in env_dict.items():
        normalized[k.upper()] = (k, str(v))

    sorted_items = sorted(normalized.values(), key=lambda pair: pair[0].upper())
    encoded_str = "\0".join(f"{k}={v}" for k, v in sorted_items) + "\0\0"
    return ctypes.create_unicode_buffer(encoded_str)


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
                ctypes.sizeof(info),
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
        h_process = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not h_process:
            return False

        try:
            return bool(kernel32.AssignProcessToJobObject(self._handle, h_process))
        finally:
            kernel32.CloseHandle(h_process)

    def terminate(self, exit_code: int = 1) -> bool:
        """Terminates all processes associated with this Job Object."""
        if sys.platform != "win32":
            return True

        if not self._handle or not self._is_active:
            return False

        kernel32 = ctypes.windll.kernel32
        return bool(kernel32.TerminateJobObject(self._handle, wintypes.UINT(exit_code)))

    def close(self) -> None:
        """Closes the Job Object handle, triggering KILL_ON_JOB_CLOSE."""
        if sys.platform == "win32" and self._handle:
            kernel32 = ctypes.windll.kernel32
            kernel32.CloseHandle(self._handle)
            self._handle = None
        self._is_active = False

    def __enter__(self):
        self.create(kill_on_close=True)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


class Win32SuspendedProcess:
    """Abstraction for a process created suspended, assigned to a Job Object, and resumed.
    Manages pipe handles, file streams, exit codes, and clean process termination.
    """

    def __init__(
        self,
        pid: int,
        h_process: wintypes.HANDLE,
        job: Win32JobObject,
        h_stdin_write: wintypes.HANDLE,
        h_stdout_read: wintypes.HANDLE,
        h_stderr_read: wintypes.HANDLE,
    ):
        self.pid = pid
        self._h_process = h_process
        self.job = job
        self._h_stdin_write = h_stdin_write
        self._h_stdout_read = h_stdout_read
        self._h_stderr_read = h_stderr_read
        self._closed = False

        if sys.platform == "win32":
            import msvcrt
            stdin_handle = self._h_stdin_write.value if hasattr(self._h_stdin_write, "value") else int(self._h_stdin_write)
            stdout_handle = self._h_stdout_read.value if hasattr(self._h_stdout_read, "value") else int(self._h_stdout_read)
            stderr_handle = self._h_stderr_read.value if hasattr(self._h_stderr_read, "value") else int(self._h_stderr_read)

            self._stdin_fd = msvcrt.open_osfhandle(stdin_handle, os.O_WRONLY)
            self._stdout_fd = msvcrt.open_osfhandle(stdout_handle, os.O_RDONLY)
            self._stderr_fd = msvcrt.open_osfhandle(stderr_handle, os.O_RDONLY)

            self.stdin = open(self._stdin_fd, "wb", buffering=0)
            self.stdout = open(self._stdout_fd, "rb", buffering=0)
            self.stderr = open(self._stderr_fd, "rb", buffering=0)
        else:
            self._stdin_fd = None
            self._stdout_fd = None
            self._stderr_fd = None
            self.stdin = None
            self.stdout = None
            self.stderr = None

    @property
    def h_process(self) -> wintypes.HANDLE:
        return self._h_process

    def poll(self) -> Optional[int]:
        """Checks if process has terminated without blocking. Returns exit code or None."""
        if sys.platform != "win32" or not self._h_process:
            return None

        kernel32 = ctypes.windll.kernel32
        wait_res = kernel32.WaitForSingleObject(self._h_process, 0)
        if wait_res == WAIT_TIMEOUT:
            return None

        exit_code = wintypes.DWORD()
        if kernel32.GetExitCodeProcess(self._h_process, ctypes.byref(exit_code)):
            if exit_code.value == STILL_ACTIVE:
                # Double-check WaitForSingleObject to confirm true still-active vs exit code 259
                if kernel32.WaitForSingleObject(self._h_process, 0) == WAIT_TIMEOUT:
                    return None
            return exit_code.value
        return None

    def wait(self, timeout_ms: int = 5000) -> Optional[int]:
        """Waits up to timeout_ms for process to exit and returns exit code."""
        if sys.platform != "win32" or not self._h_process:
            return None

        kernel32 = ctypes.windll.kernel32
        wait_res = kernel32.WaitForSingleObject(self._h_process, wintypes.DWORD(timeout_ms))
        if wait_res == WAIT_TIMEOUT:
            return None

        exit_code = wintypes.DWORD()
        if kernel32.GetExitCodeProcess(self._h_process, ctypes.byref(exit_code)):
            return exit_code.value
        return None

    def terminate(self, exit_code: int = 1) -> bool:
        """Terminates the process and its job object container."""
        if sys.platform != "win32" or not self._h_process:
            return True

        kernel32 = ctypes.windll.kernel32
        # Terminate job object tree first if available
        if self.job and self.job.is_active:
            self.job.terminate(exit_code=exit_code)
        # Terminate root process directly as well
        return bool(kernel32.TerminateProcess(self._h_process, wintypes.UINT(exit_code)))

    def close(self) -> None:
        """Deterministically closes all open file handles and the process handle."""
        if self._closed:
            return
        self._closed = True

        # Close Python file wrappers
        for stream in (self.stdin, self.stdout, self.stderr):
            if stream:
                try:
                    stream.close()
                except Exception:
                    pass

        # Close process handle
        if sys.platform == "win32" and self._h_process:
            kernel32 = ctypes.windll.kernel32
            kernel32.CloseHandle(self._h_process)
            self._h_process = None


class Win32SuspendedLauncher:
    """Creates child processes in CREATE_SUSPENDED state with explicit handle inheritance
    (STARTUPINFOEXW + PROC_THREAD_ATTRIBUTE_HANDLE_LIST), assigns to a Win32 Job Object,
    verifies assignment, and resumes execution. Guarantees zero child breakaway races.
    """

    @staticmethod
    def launch(
        command: str,
        args: List[str],
        env: Dict[str, str],
        cwd: Optional[str] = None,
        job: Optional[Win32JobObject] = None,
        job_name: Optional[str] = None,
        no_window: bool = True,
    ) -> Win32SuspendedProcess:
        """Launches a child process suspended, binds it to a Job Object, and resumes it."""
        if sys.platform != "win32":
            raise NotImplementedError("Win32SuspendedLauncher requires a Windows platform.")

        import shutil
        import subprocess

        kernel32 = ctypes.windll.kernel32

        # 1. Resolve executable path
        resolved_exe = shutil.which(command)
        if not resolved_exe:
            if os.path.isabs(command) and os.path.exists(command):
                resolved_exe = command
            else:
                raise FileNotFoundError(f"Cannot resolve executable command '{command}'")

        # 2. Validate working directory
        if cwd and not os.path.isdir(cwd):
            raise FileNotFoundError(f"Specified working directory does not exist: '{cwd}'")

        # 3. Format command line buffer
        full_args = [resolved_exe] + list(args)
        cmdline_str = subprocess.list2cmdline(full_args)
        cmdline_buf = ctypes.create_unicode_buffer(cmdline_str)

        # 4. Prepare redirected pipes with security attributes
        sa = SECURITY_ATTRIBUTES()
        sa.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
        sa.bInheritHandle = True
        sa.lpSecurityDescriptor = None

        h_stdin_read = wintypes.HANDLE()
        h_stdin_write = wintypes.HANDLE()
        if not kernel32.CreatePipe(ctypes.byref(h_stdin_read), ctypes.byref(h_stdin_write), ctypes.byref(sa), 0):
            raise OSError(f"Failed to create stdin pipe: error {kernel32.GetLastError()}")
        kernel32.SetHandleInformation(h_stdin_write, HANDLE_FLAG_INHERIT, 0)

        h_stdout_read = wintypes.HANDLE()
        h_stdout_write = wintypes.HANDLE()
        if not kernel32.CreatePipe(ctypes.byref(h_stdout_read), ctypes.byref(h_stdout_write), ctypes.byref(sa), 0):
            kernel32.CloseHandle(h_stdin_read)
            kernel32.CloseHandle(h_stdin_write)
            raise OSError(f"Failed to create stdout pipe: error {kernel32.GetLastError()}")
        kernel32.SetHandleInformation(h_stdout_read, HANDLE_FLAG_INHERIT, 0)

        h_stderr_read = wintypes.HANDLE()
        h_stderr_write = wintypes.HANDLE()
        if not kernel32.CreatePipe(ctypes.byref(h_stderr_read), ctypes.byref(h_stderr_write), ctypes.byref(sa), 0):
            kernel32.CloseHandle(h_stdin_read)
            kernel32.CloseHandle(h_stdin_write)
            kernel32.CloseHandle(h_stdout_read)
            kernel32.CloseHandle(h_stdout_write)
            raise OSError(f"Failed to create stderr pipe: error {kernel32.GetLastError()}")
        kernel32.SetHandleInformation(h_stderr_read, HANDLE_FLAG_INHERIT, 0)

        # 5. Initialize STARTUPINFOEXW and attribute list for explicit handle inheritance
        attr_size = ctypes.c_size_t()
        kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(attr_size))
        attr_buf = ctypes.create_string_buffer(attr_size.value)
        p_attr_list = ctypes.cast(attr_buf, ctypes.c_void_p)

        if not kernel32.InitializeProcThreadAttributeList(p_attr_list, 1, 0, ctypes.byref(attr_size)):
            err = kernel32.GetLastError()
            for h in (h_stdin_read, h_stdin_write, h_stdout_read, h_stdout_write, h_stderr_read, h_stderr_write):
                kernel32.CloseHandle(h)
            raise OSError(f"InitializeProcThreadAttributeList failed: error {err}")

        # Explicit allowlist of inherited handles: strictly only the 3 child pipe endpoints
        handles_array = (wintypes.HANDLE * 3)(h_stdin_read, h_stdout_write, h_stderr_write)
        if not kernel32.UpdateProcThreadAttribute(
            p_attr_list,
            0,
            PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
            handles_array,
            ctypes.sizeof(handles_array),
            None,
            None,
        ):
            err = kernel32.GetLastError()
            kernel32.DeleteProcThreadAttributeList(p_attr_list)
            for h in (h_stdin_read, h_stdin_write, h_stdout_read, h_stdout_write, h_stderr_read, h_stderr_write):
                kernel32.CloseHandle(h)
            raise OSError(f"UpdateProcThreadAttribute failed: error {err}")

        si_ex = STARTUPINFOEXW()
        si_ex.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
        si_ex.StartupInfo.dwFlags = STARTF_USESTDHANDLES
        si_ex.StartupInfo.hStdInput = h_stdin_read
        si_ex.StartupInfo.hStdOutput = h_stdout_write
        si_ex.StartupInfo.hStdError = h_stderr_write
        si_ex.lpAttributeList = p_attr_list

        # 6. Build Unicode environment block
        env_buf = create_unicode_environment_block(env)

        # 7. Prepare Job Object
        active_job = job
        if not active_job:
            jname = job_name or f"Presence_Job_{int(time.time()*1000)}"
            active_job = Win32JobObject(name=jname)
            if not active_job.create(kill_on_close=True):
                err = kernel32.GetLastError()
                kernel32.DeleteProcThreadAttributeList(p_attr_list)
                for h in (h_stdin_read, h_stdin_write, h_stdout_read, h_stdout_write, h_stderr_read, h_stderr_write):
                    kernel32.CloseHandle(h)
                raise RuntimeError(f"Failed to create Job Object '{jname}': error {err}")

        # 8. Create Process Suspended
        pi = PROCESS_INFORMATION()
        creation_flags = CREATE_SUSPENDED | EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT
        if no_window:
            creation_flags |= CREATE_NO_WINDOW

        success = kernel32.CreateProcessW(
            resolved_exe,
            cmdline_buf,
            None,
            None,
            True,  # bInheritHandles (restricted to handle list)
            creation_flags,
            ctypes.byref(env_buf),
            cwd,
            ctypes.byref(si_ex),
            ctypes.byref(pi),
        )

        # Child handles in parent and attribute list can now be released
        kernel32.CloseHandle(h_stdin_read)
        kernel32.CloseHandle(h_stdout_write)
        kernel32.CloseHandle(h_stderr_write)
        kernel32.DeleteProcThreadAttributeList(p_attr_list)

        if not success:
            err = kernel32.GetLastError()
            kernel32.CloseHandle(h_stdin_write)
            kernel32.CloseHandle(h_stdout_read)
            kernel32.CloseHandle(h_stderr_read)
            raise OSError(f"CreateProcessW failed for '{resolved_exe}': Win32 error {err}")

        # 9. Assign Process to Job Object BEFORE resuming
        assigned = kernel32.AssignProcessToJobObject(active_job.handle, pi.hProcess)
        if not assigned:
            err = kernel32.GetLastError()
            kernel32.TerminateProcess(pi.hProcess, 1)
            kernel32.CloseHandle(pi.hThread)
            kernel32.CloseHandle(pi.hProcess)
            kernel32.CloseHandle(h_stdin_write)
            kernel32.CloseHandle(h_stdout_read)
            kernel32.CloseHandle(h_stderr_read)
            raise RuntimeError(f"AssignProcessToJobObject failed for PID {pi.dwProcessId}: Win32 error {err}. Process terminated safely.")

        # 10. Verify Assignment using IsProcessInJob
        is_in_job = wintypes.BOOL()
        if kernel32.IsProcessInJob(pi.hProcess, active_job.handle, ctypes.byref(is_in_job)):
            if not is_in_job:
                kernel32.TerminateProcess(pi.hProcess, 1)
                kernel32.CloseHandle(pi.hThread)
                kernel32.CloseHandle(pi.hProcess)
                kernel32.CloseHandle(h_stdin_write)
                kernel32.CloseHandle(h_stdout_read)
                kernel32.CloseHandle(h_stderr_read)
                raise RuntimeError(f"Security invariant violated: Process {pi.dwProcessId} is not member of Job Object '{active_job.name}'. Terminated.")

        # 11. Resume primary thread
        prev_suspend_count = kernel32.ResumeThread(pi.hThread)
        kernel32.CloseHandle(pi.hThread)

        if prev_suspend_count == 0xFFFFFFFF:
            err = kernel32.GetLastError()
            kernel32.TerminateProcess(pi.hProcess, 1)
            kernel32.CloseHandle(pi.hProcess)
            kernel32.CloseHandle(h_stdin_write)
            kernel32.CloseHandle(h_stdout_read)
            kernel32.CloseHandle(h_stderr_read)
            raise RuntimeError(f"ResumeThread failed for PID {pi.dwProcessId}: Win32 error {err}. Process terminated.")

        # 12. Return managed process instance
        return Win32SuspendedProcess(
            pid=pi.dwProcessId,
            h_process=pi.hProcess,
            job=active_job,
            h_stdin_write=h_stdin_write,
            h_stdout_read=h_stdout_read,
            h_stderr_read=h_stderr_read,
        )


