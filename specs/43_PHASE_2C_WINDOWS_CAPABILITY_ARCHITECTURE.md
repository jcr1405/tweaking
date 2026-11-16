# SPECIFICATION 43 — PHASE 2C: WINDOWS CAPABILITY ARCHITECTURE (CORRECTED)
# Persistent Intelligent Presence — Native Windows Capabilities, ToolRegistry Dispatch & Execution Guardrails

## 1. Executive Summary & Objective

Phase 2C introduces controlled execution of Windows-native capabilities into Presence. 

While context acquisition (Spec 41) provides ambient desktop awareness, capabilities allow the agent to perform useful, authorized operations within Windows.

To preserve the security, reliability, and architectural integrity of the backend, the capability architecture adheres to four non-negotiable principles:
1. **Zero Second Permission System (P1-07)**: All native capabilities register directly as tools within the existing `ToolRegistry` (`runtime/tools/registry.py`) and are authorized through the existing `PermissionBoundary` (`runtime/permissions.py`) using the verified `AuthorityLevel` enum.
2. **Strict Separation of Observation and Execution**: Observation capabilities (inspecting metadata) never grant or imply execution authority (modifying system state).
3. **Respect for Windows Security & Shell Architecture (P0-05, P0-06)**: Security zone checks are preserved (no `SEE_MASK_NOZONECHECKS`); Windows foreground activation policies are respected (no `AttachThreadInput` hacks); and shell operations are clearly distinguished from contained process launches.
4. **Accurate Process Containment Terminology (P1-08)**: Job Objects provide process lifecycle and resource management; they are **not** treated as complete security sandboxes.

---

## 2. Integration with Existing ToolRegistry & PermissionBoundary (P1-07)

Native capabilities register directly into `ToolRegistry` using the exact existing Python classes:

```
[AgentExecutor]
       │
       │ 1. Invokes tool by ID (e.g. "desktop.activate_window")
       ▼
[ToolRegistry] (runtime/tools/registry.py)
       │
       │ 2. Verifies registration & retrieves AuthorityLevel
       ▼
[PermissionBoundary] (runtime/permissions.py)
       │
       │ 3. Enforces L0/L1/L2/L3 permissions & validates ConsentToken (if L3)
       ▼
[Native Capability Handler] (runtime/capabilities/windows/)
       │
       │ 4. Executes native Win32 / Shell API (Class A, B, or C)
       ▼
[Structured Result Envelope]
       │
       │ 5. Returns {"status": "SUCCESS" | "ERROR", "result": ..., "error": ...}
       ▼
[Trajectory & Event Journal] (runtime/trajectory.py)
```

### 2.1 Exact ToolDefinition Interface Alignment

In the existing codebase (`runtime/tools/registry.py`), `ToolDefinition` is defined as:

```python
@dataclass
class ToolDefinition:
    tool_id: str
    name: str
    description: str
    parameters: Dict[str, Any]
    authority_tier: PermissionTier    # Alias for AuthorityLevel
    handler: Callable[[Dict[str, Any]], Dict[str, Any]]
    timeout_seconds: float = 10.0
```

All Phase 2C capabilities use the exact existing enum values from `runtime/permissions.py`:
- `AuthorityLevel.L0_OBSERVE` (Observation, read-only telemetry)
- `AuthorityLevel.L1_RECOMMEND` (Synthesizing recommendations, diffs)
- `AuthorityLevel.L2_EXECUTE_REVERSIBLE` (Reversible operations, window focus)
- `AuthorityLevel.L3_PROTECTED` (Irreversible actions, credentials)

---

## 3. Initial Phase 2C Capability Inventory

Phase 2C defines an initial set of 6 foundational capabilities. Capabilities requiring unverified security paths (such as ambient clipboard access) are explicitly deferred:

| Tool Identifier | Category | Action Type | Authority Level | Risk Level | Reversible | Class | Timeout | Primary Native API |
|---|---|---|---|---|---|---|---|---|
| `system.get_runtime_status` | Observation | Query Presence runtime health | `L0_OBSERVE` | Negligible | N/A | Class A | 1.0s | Internal telemetry |
| `system.get_resources` | Observation | Query CPU/RAM/Power state | `L0_OBSERVE` | Negligible | N/A | Class A | 1.0s | `GlobalMemoryStatusEx` |
| `desktop.get_foreground_window` | Observation | Read active window caption/PID | `L0_OBSERVE` | Low | N/A | Class A | 1.5s | `GetForegroundWindow` / `GetWindowTextW` |
| `desktop.list_open_windows` | Observation | List visible top-level windows | `L0_OBSERVE` | Low | N/A | Class A | 2.0s | `EnumWindows` with cloaking filter |
| `desktop.activate_window` | Execution | Best-effort window foreground | `L2_EXECUTE_REVERSIBLE` | Medium | Yes | Class A | 2.0s | `SetForegroundWindow` (with TOCTOU guard) |
| `shell.open_path` | Execution | Open file/folder in default app | `L2_EXECUTE_REVERSIBLE` | High | Yes | Class C | 5.0s (wrapper) | `ShellExecuteExW` (zone checks preserved) |

> [!NOTE]
> **Clipboard Reading Deferred (P1-06)**: `presence.read_clipboard` is removed from default Phase 2C tools. It is deferred to a future milestone with a dedicated, trusted user-consent interface.

---

## 4. Capability Specifications & Native Implementation Contracts

### 4.1 System Observation Capabilities (`AuthorityLevel.L0_OBSERVE`)

#### `desktop.get_foreground_window`
- **Description**: Returns metadata for the currently active top-level window.
- **Parameters**: `{"type": "object", "properties": {}, "additionalProperties": False}`
- **Native Implementation**: Calls `GetForegroundWindow()`, `GetWindowTextW()`, `GetClassNameW()`, `GetWindowThreadProcessId()`, and `QueryFullProcessImageNameW()`. Fast, non-blocking Class A operation.
- **Output Envelope**:
  ```json
  {
    "status": "SUCCESS",
    "result": {
      "hwnd": 131244,
      "title": "settings.py - Visual Studio Code",
      "application_name": "Code.exe",
      "executable_path": "C:\\Program Files\\Microsoft VS Code\\Code.exe",
      "process_id": 14220
    }
  }
  ```

#### `desktop.list_open_windows`
- **Description**: Enumerates top-level visible desktop application windows.
- **Filtering Rules**: Filters out cloaked windows (`DwmGetWindowAttribute(DWMWA_CLOAKED)`), tool windows (`WS_EX_TOOLWINDOW`), zero-rect windows, and desktop shells. Bounded to a maximum of 25 entries.

---

### 4.2 Reversible Desktop Execution Capabilities (`AuthorityLevel.L2_EXECUTE_REVERSIBLE`)

#### `desktop.activate_window` (P0-06, P2C-18)
- **Description**: Requests Windows to bring an existing top-level application window to the foreground.
- **Parameters Schema**:
  ```json
  {
    "type": "object",
    "properties": {
      "hwnd": {"type": "integer", "description": "Target window HWND"},
      "expected_process_id": {"type": "integer", "description": "Target process ID for TOCTOU verification"},
      "expected_creation_filetime_ticks": {"type": "integer", "description": "Target process start time as 64-bit FILETIME ticks (100ns intervals since 1601-01-01 UTC) for TOCTOU verification"}
    },
    "required": ["hwnd", "expected_process_id"]
  }
  ```
- **Accurate Windows Foreground Behavior (P0-06)**:
  - Windows restricts arbitrary processes from stealing the foreground via `SetForegroundWindow` ([MSDN Documentation](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setforegroundwindow)).
  - Presence **rejects** `AttachThreadInput` hacks, which cause UI lockups, thread message corruption, and input deadlocks.
  - Presence implements `desktop.activate_window` as a **best-effort operation**:
    1. Validate `IsWindow(hwnd)`.
    2. Validate that current PID matches `expected_process_id`.
    3. Validate process creation time via `GetProcessTimes` if `expected_creation_filetime_ticks` is provided. The `creation_time` FILETIME struct from `GetProcessTimes` is converted to a 64-bit unsigned integer and compared directly with raw integer equality against `expected_creation_filetime_ticks`. Zero float conversion occurs, and `datetime` is never used for TOCTOU validation.
    4. Call `SetForegroundWindow(hwnd)`.
    5. If `SetForegroundWindow` returns `FALSE` (foreground activation denied by Windows policy), call `FlashWindowEx()` to alert the user on the taskbar, and return status `ACTIVATION_QUEUED_OR_FLASHED`.
- **Return Value**:
  ```json
  {
    "status": "SUCCESS",
    "result": {
      "activated": true,
      "mechanism": "SET_FOREGROUND_WINDOW"
    }
  }
  ```
  Or if denied by OS foreground lock:
  ```json
  {
    "status": "SUCCESS",
    "result": {
      "activated": false,
      "mechanism": "FLASHED_TASKBAR",
      "notice": "Windows foreground lock prevented immediate focus. Taskbar icon flashed."
    }
  }
  ```

#### `shell.open_path` (P0-05, P1-08, P2C-17)
- **Description**: Opens an approved file or directory path using the standard registered Windows shell association handler.
- **Security Corrections (P0-05, P2C-17)**:
  - **`SEE_MASK_NOZONECHECKS` is PROHIBITED**: Presence strictly respects Windows security zones and attachment execution policies (`IAttachmentExecute`). Setting `0x00000008` is completely forbidden.
  - **Executable Blacklisting**: Directly opening executable files (`.exe`, `.bat`, `.cmd`, `.vbs`, `.ps1`, `.scr`, `.msi`) is **rejected** by this tool. Executable launching must be handled by an explicit application launcher.
  - **Path Validation**: Paths must be normalized via `os.path.realpath()` and verified to exist before invocation. System directories (`C:\Windows\System32`, `C:\ProgramData`) are blocked.
- **ShellExecute Flag Constants & Semantics (P2C-17)**:
  - `SEE_MASK_NOCLOSEPROCESS = 0x00000040`: Requests a process handle in `SHELLEXECUTEINFOW.hProcess` if a new process was created. Note: Previous draft mistakenly labeled `0x00000040` as `SEE_MASK_FLAG_NO_UI`.
  - `SEE_MASK_FLAG_NO_UI = 0x00000400`: Suppresses ordinary shell error message boxes (e.g. file not found, bad association). Crucially, Windows security prompts (such as Mark-of-the-Web and Attachment Execution Service confirmation) remain exempt by operating system design and are still presented when required.
  - `SEE_MASK_NOZONECHECKS = 0x00000008`: PROHIBITED. Must never be set.
  - `SEE_MASK_NOASYNC = 0x00000100`: Forces synchronous execution on the calling thread. Does *not* provide a timeout; it makes the call block until shell initialization completes.
  - `SEE_MASK_ASYNCOK = 0x00100000`: Allows asynchronous execution on a background thread created by the Windows shell.
- **Process Handle Availability & Caller Ownership (P2C-17)**:
  - `SEE_MASK_NOCLOSEPROCESS` only populates `hProcess` if a new process instance was spawned.
  - If the shell operation reuses an existing process (e.g. opens a new tab in an already running browser or Explorer instance), or delegates to DDE or an in-process COM handler, `hProcess` is `NULL` (`0` or `None`).
  - **Ownership Contract**: If `hProcess` is non-NULL, the caller owns the handle and is strictly responsible for closing it via `CloseHandle()`. Leaking `hProcess` leaks kernel handles.
  - Applications launched via `ShellExecuteExW` run in the user's interactive desktop session outside Presence's Win32 Job Object containment hierarchy.
- **Execution Classification & Timeout Policy (P2C-17)**:
  - `ShellExecuteExW` does NOT accept a native timeout parameter.
  - Reclassified from Class B to **Class C (Best-Effort Asynchronous Shell Request)**.
  - Invoked with `SEE_MASK_FLAG_NO_UI | SEE_MASK_ASYNCOK | SEE_MASK_NOCLOSEPROCESS`.
  - The Python runtime wraps the execution with `anyio.fail_after(5.0)`. If the timeout expires, the Python coroutine cancels, but the underlying worker thread remains until the shell call returns.
- **Implementation**:
  ```python
  import ctypes
  from ctypes import wintypes

  SEE_MASK_NOCLOSEPROCESS = 0x00000040  # Request hProcess if new process spawned
  SEE_MASK_FLAG_NO_UI     = 0x00000400  # Suppress ordinary error dialogs; security prompts exempt
  SEE_MASK_NOZONECHECKS   = 0x00000008  # PROHIBITED: must never be used
  SEE_MASK_NOASYNC        = 0x00000100  # Forces synchronous completion (NOT a timeout)
  SEE_MASK_ASYNCOK        = 0x00100000  # Permits shell async dispatch

  class SHELLEXECUTEINFOW(ctypes.Structure):
      _fields_ = [
          ("cbSize", wintypes.DWORD),
          ("fMask", wintypes.ULONG),
          ("hwnd", wintypes.HWND),
          ("lpVerb", wintypes.LPCWSTR),
          ("lpFile", wintypes.LPCWSTR),
          ("lpParameters", wintypes.LPCWSTR),
          ("lpDirectory", wintypes.LPCWSTR),
          ("nShow", ctypes.c_int),
          ("hInstApp", wintypes.HINSTANCE),
          ("lpIDList", wintypes.LPVOID),
          ("lpClass", wintypes.LPCWSTR),
          ("hkeyClass", wintypes.HKEY),
          ("dwHotKey", wintypes.DWORD),
          ("hIconOrMonitor", wintypes.HANDLE),
          ("hProcess", wintypes.HANDLE),
      ]

  sei = SHELLEXECUTEINFOW()
  sei.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
  # Correct flags: suppress non-security UI, permit async shell dispatch, request hProcess
  sei.fMask = SEE_MASK_FLAG_NO_UI | SEE_MASK_ASYNCOK | SEE_MASK_NOCLOSEPROCESS
  sei.lpVerb = "open"
  sei.lpFile = normalized_path
  sei.nShow = 1  # SW_SHOWNORMAL

  success = ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei))
  if not success:
      err = ctypes.GetLastError()
      raise ctypes.WinError(err)

  # Caller ownership: close hProcess if a new process handle was created
  if sei.hProcess:
      try:
          ctypes.windll.kernel32.CloseHandle(sei.hProcess)
      except Exception:
          pass
  ```

---

## 5. Timeout & Cancellation Architecture (P0-07)

### 5.1 Python Worker Thread Cancellation Reality
In AnyIO / Python, `anyio.fail_after()` combined with `anyio.to_thread.run_sync()` cancels the *awaiting coroutine*, but **does not forcibly terminate the underlying synchronous OS worker thread**. If a native C/Win32 call blocks inside the Windows kernel, the worker thread remains blocked until the call completes or the process terminates.

### 5.2 Native Operation Classification

Presence classifies every native capability call into four strict execution classes:

```
+---------------------------------------------------------------------------------------------------+
|                               NATIVE OPERATION CLASSIFICATION                                     |
+------------------------------------+--------------------------------------------------------------+
| CLASS                              | OPERATIONAL CHARACTERISTICS & SAFETY GUARANTEES              |
+------------------------------------+--------------------------------------------------------------+
| Class A: Naturally Bounded Native  | Execution duration is naturally bounded to microseconds or   |
|                                    | milliseconds. Cannot block. Examples: `GetForegroundWindow`, |
|                                    | `GetWindowTextW` (top-level), `GetClassNameW`, `OpenProcess`.|
+------------------------------------+--------------------------------------------------------------+
| Class B: Native API with Timeout   | The Win32 API accepts an explicit timeout parameter enforced |
|                                    | by the Windows kernel. Example: `SendMessageTimeoutW`.       |
+------------------------------------+--------------------------------------------------------------+
| Class C: Cooperative / Shell Async | Operation is either a cooperative worker loop inspecting     |
|         Best-Effort Request        | cancellation tokens, OR an asynchronous best-effort shell    |
|                                    | request (`ShellExecuteExW` with `SEE_MASK_ASYNCOK`). Shell   |
|                                    | execution dispatch is handed off to OS background threads;   |
|                                    | Python wraps the call with a cooperative cancellation timeout|
|                                    | (`anyio.fail_after`). The underlying worker thread cannot be |
|                                    | forcibly aborted without process termination if the shell    |
|                                    | blocks inside an external COM server.                        |
+------------------------------------+--------------------------------------------------------------+
| Class D: Potentially Unbounded     | Native call may hang indefinitely without internal timeout.  |
|                                    | Examples: Full COM UI Automation tree walking, blocking DDE. |
|                                    | Presence Rule: STRICTLY PROHIBITED in the main runtime thread|
|                                    | pool. Must be supervised in a separate Job-contained process |
|                                    | or omitted from Phase 2C.                                    |
+------------------------------------+--------------------------------------------------------------+
```

Every native capability in Phase 2C is restricted to **Class A or Class C**. Zero Class D operations are permitted in the in-process capability pool.

---

## 6. Registration Interface Example

```python
from runtime.tools.registry import ToolRegistry, ToolDefinition
from runtime.permissions import AuthorityLevel

def register_windows_capabilities(registry: ToolRegistry) -> None:
    """Registers Phase 2C native Windows capabilities into ToolRegistry."""
    
    # 1. desktop.get_foreground_window (Class A)
    registry.register(
        ToolDefinition(
            tool_id="desktop.get_foreground_window",
            name="get_foreground_window",
            description="Retrieves metadata for the current foreground window.",
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            authority_tier=AuthorityLevel.L0_OBSERVE,
            handler=desktop_get_foreground_window_handler,
            timeout_seconds=1.5,
        )
    )
    
    # 2. desktop.activate_window (Class A - Best Effort)
    registry.register(
        ToolDefinition(
            tool_id="desktop.activate_window",
            name="activate_window",
            description="Brings an open window to the foreground with TOCTOU verification.",
            parameters={
                "type": "object",
                "properties": {
                    "hwnd": {"type": "integer", "description": "Target window HWND"},
                    "expected_process_id": {"type": "integer", "description": "Target PID for TOCTOU validation"},
                    "expected_creation_filetime_ticks": {"type": "integer", "description": "Target process start time as 64-bit FILETIME ticks (100ns intervals since 1601-01-01 UTC)"}
                },
                "required": ["hwnd", "expected_process_id"],
                "additionalProperties": False,
            },
            authority_tier=AuthorityLevel.L2_EXECUTE_REVERSIBLE,
            handler=desktop_activate_window_handler,
            timeout_seconds=2.0,
        )
    )
    
    # 3. shell.open_path (Class C - Asynchronous Best-Effort Request, Zone Checked)
    registry.register(
        ToolDefinition(
            tool_id="shell.open_path",
            name="open_path",
            description="Opens an approved non-executable path using default Windows handler.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to file or folder to open"}
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            authority_tier=AuthorityLevel.L2_EXECUTE_REVERSIBLE,
            handler=shell_open_path_handler,
            timeout_seconds=5.0,
        )
    )
```

---

## 7. Architectural Verification & Acceptance Criteria

Specification 43 is ready for implementation only when all following criteria are satisfied:
1. **Existing Codebase Contract Compliance**: Uses exact `AuthorityLevel` enums and `ToolDefinition` dataclass fields without creating duplicate schemas.
2. **Zone Checks Preserved**: `SEE_MASK_NOZONECHECKS` is completely absent; zone safety is preserved.
3. **Shell vs Job Object Disambiguation**: Clear architectural distinction between shell association opening and Job-contained process execution.
4. **Best-Effort Foreground Activation**: `desktop.activate_window` adheres to Windows foreground policy and implements composite TOCTOU verification without `AttachThreadInput`.
5. **Worker Thread Boundedness & Classification**: All capability functions are strictly Class A or Class C; no Class D blocking calls exist. Shell requests are correctly classified as Class C asynchronous best-effort requests with caller-owned handle management.
6. **Canonical Process Identity**: Window identity and TOCTOU validation use canonical 64-bit FILETIME ticks (`expected_creation_filetime_ticks`) compared strictly via raw 64-bit integer equality without float conversions or datetime approximations.
