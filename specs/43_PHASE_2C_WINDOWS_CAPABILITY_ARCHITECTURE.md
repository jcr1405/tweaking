# SPECIFICATION 43 — PHASE 2C: WINDOWS CAPABILITY ARCHITECTURE
# Persistent Intelligent Presence — Native Windows Capabilities, ToolRegistry Dispatch & Reversible Actions

## 1. Executive Summary & Objective

In Phase 2C, Presence introduces controlled execution of Windows-native capabilities. 

Context acquisition (Spec 41) provides the agent with situational awareness. Capabilities provide the agent with the ability to act upon the Windows desktop. 

To preserve the security and reliability of the backend, the capability architecture adheres to four non-negotiable architectural principles:
1. **Zero Second Permission System**: All native capabilities register directly as tools within the existing `ToolRegistry` (`runtime/tools/registry.py`) and are authorized through the existing `PermissionBoundary` (`runtime/permissions.py`).
2. **Strict Separation of Observation and Execution**: Observation capabilities (inspecting metadata) never grant or imply execution authority (modifying system state).
3. **Deterministic Authorization**: The LLM cannot grant its own consent, escalate its privilege tier, forge consent tokens, or execute native Windows APIs directly.
4. **Reversible Actions Preferred**: Initial desktop actions focus on safe, reversible, or user-confirmed operations (e.g. focusing an existing window or opening an approved file path) rather than destructive mutations.

---

## 2. Capability Architecture & Integration Flow

The native capability subsystem (`runtime/capabilities/`) integrates directly into the existing agent execution pipeline:

```
[AgentExecutor]
       │
       │ 1. Invokes tool by name (e.g. "desktop.activate_window")
       ▼
[ToolRegistry] (runtime/tools/registry.py)
       │
       │ 2. Verifies registration & retrieves AuthorityLevel
       ▼
[PermissionBoundary] (runtime/permissions.py)
       │
       │ 3. Enforces L0/L1/L2/L3 permissions & validates ConsentToken (if L3)
       ▼
[CapabilityDispatcher] (runtime/capabilities/dispatcher.py)
       │
       │ 4. Routes call to registered native adapter with thread timeout
       ▼
[NativeWindowsAdapter] (runtime/capabilities/windows/)
       │
       │ 5. Executes native Win32 / Shell API via ctypes
       ▼
[Structured Result Envelope]
       │
       │ 6. Normalized JSON-serializable dictionary returned to AgentExecutor
       ▼
[Trajectory & Audit Log] (runtime/trajectory.py)
```

### 2.1 Core Architectural Invariant
Native capabilities are **never** executed by arbitrary Python shell scripts, uncontained subprocesses, or monkey-patched libraries. Every capability is an explicit, typed, audited, and isolated Python function registered in `ToolRegistry`.

---

## 3. Initial Phase 2C Capability Inventory & Risk Ranking

Phase 2C defines an initial set of 8 foundational capabilities. Each capability is strictly classified and ranked:

| Tool Identifier | Category | Action Type | Authority Tier | Risk Level | Reversible | Timeout | Primary Win32 API |
|---|---|---|---|---|---|---|---|
| `system.get_runtime_status` | Observation | Query Presence runtime health | `L0_OBSERVATION` | Negligible | N/A | 1.0s | Internal telemetry |
| `system.get_resources` | Observation | Query CPU/RAM/Battery state | `L0_OBSERVATION` | Negligible | N/A | 1.0s | `GlobalMemoryStatusEx` |
| `desktop.get_foreground_window` | Observation | Read active window metadata | `L0_OBSERVATION` | Low | N/A | 1.5s | `GetForegroundWindow` |
| `desktop.list_open_windows` | Observation | List visible top-level windows | `L0_OBSERVATION` | Low | N/A | 2.0s | `EnumWindows` |
| `desktop.activate_window` | Execution | Bring existing window to focus | `L2_EXECUTION` | Medium | Yes | 2.0s | `SetForegroundWindow` |
| `shell.open_path` | Execution | Open approved file or folder | `L2_EXECUTION` | High | Yes | 5.0s | `ShellExecuteExW` |
| `shell.open_application` | Execution | Launch approved executable | `L2_EXECUTION` | High | Yes | 5.0s | Win32 Job Object Launcher |
| `presence.read_clipboard` | User-Assisted | Read plain text from clipboard | `L3_PROTECTED` | Critical | N/A | 1.0s | `GetClipboardData` |

---

## 4. Capability Specifications & Contracts

### 4.1 System Observation Capabilities (`L0_OBSERVATION`)

#### `desktop.get_foreground_window`
- **Description**: Returns metadata for the currently active foreground window.
- **Input Schema**:
  ```json
  {
    "type": "object",
    "properties": {},
    "additionalProperties": false
  }
  ```
- **Output Schema**:
  ```json
  {
    "type": "object",
    "properties": {
      "hwnd": {"type": "integer"},
      "title": {"type": "string"},
      "application_name": {"type": "string"},
      "executable_path": {"type": "string"},
      "process_id": {"type": "integer"}
    },
    "required": ["hwnd", "title", "application_name", "process_id"]
  }
  ```
- **Execution Mechanism**: Direct Win32 call with 150 ms window text timeout. Thread-safe.

#### `desktop.list_open_windows`
- **Description**: Returns a filtered list of top-level visible desktop application windows.
- **Filtering Rules**: Ignores cloaked windows (`DwmGetWindowAttribute(DWMWA_CLOAKED)`), zero-size windows, tool windows (`WS_EX_TOOLWINDOW`), and system shells. Max output: 25 windows.
- **Output Schema**: List of `{hwnd, title, application_name, process_id}`.

### 4.2 Reversible Desktop Execution Capabilities (`L2_EXECUTION`)

#### `desktop.activate_window`
- **Description**: Activates and brings an existing top-level application window to the foreground.
- **Input Schema**:
  ```json
  {
    "type": "object",
    "properties": {
      "hwnd": {"type": "integer", "description": "HWND of the target window"},
      "expected_process_id": {"type": "integer", "description": "Safety verification PID"}
    },
    "required": ["hwnd"]
  }
  ```
- **Reversibility**: **High**. Activating a window changes focus but does not destroy or modify any data. To prevent focus-stealing issues, Presence uses `AttachThreadInput` + `SetForegroundWindow` with validation that `expected_process_id` matches the current window owner (TOCTOU protection).

#### `shell.open_path`
- **Description**: Opens a specified file or directory path using the default registered Windows handler.
- **Security Guardrails**:
  1. *Path Normalization*: Path is resolved using `PathCanonicalizeW` and `os.path.realpath()`.
  2. *Executable Blacklist*: Directly opening executable files (`.exe`, `.bat`, `.cmd`, `.vbs`, `.ps1`, `.scr`) is strictly rejected by this tool (must use `shell.open_application` with process containment).
  3. *Path Restriction*: System directories (`C:\Windows\System32`, `C:\Program Files`, AppData root) are rejected without explicit L3 consent.
- **Native Implementation**: Uses `ShellExecuteExW` with `SEE_MASK_FLAG_NO_UI | SEE_MASK_NOZONECHECKS`.

#### `shell.open_application`
- **Description**: Launches an approved application executable into an isolated Win32 Job Object.
- **Containment Guarantee**: The launched application is spawned via `Win32SuspendedLauncher` (established in Phase 2B.1), assigned to an application Job Object with resource limits, and released into execution.

### 4.3 User-Assisted Protected Capabilities (`L3_PROTECTED`)

#### `presence.read_clipboard`
- **Description**: Reads the current plain-text contents of the Windows clipboard.
- **Authorization Barrier**: Level `L3_PROTECTED`. Requires a valid `ConsentToken` bound cryptographically to the action and caller turn.
- **Guarantees**: Never called in a loop. Only retrieves `CF_UNICODETEXT`. Releases clipboard handle immediately via `CloseClipboard`.

---

## 5. ToolRegistry Integration & Deterministic Dispatch

Native capabilities register into `ToolRegistry` using a standardized registration interface:

```python
from runtime.tools.registry import ToolRegistry, ToolDefinition
from runtime.permissions import AuthorityLevel

def register_windows_capabilities(registry: ToolRegistry) -> None:
    """Registers Phase 2C native Windows capabilities into ToolRegistry."""
    
    registry.register(
        ToolDefinition(
            name="desktop.get_foreground_window",
            description="Retrieves metadata for the current foreground window.",
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=desktop_get_foreground_window_handler,
            authority_level=AuthorityLevel.L0_OBSERVATION,
            is_async=True,
            timeout_seconds=1.5,
        )
    )
    
    registry.register(
        ToolDefinition(
            name="desktop.activate_window",
            description="Brings an open window to the foreground.",
            parameters={
                "type": "object",
                "properties": {
                    "hwnd": {"type": "integer", "description": "Target window HWND"},
                    "expected_process_id": {"type": "integer", "description": "Target PID"}
                },
                "required": ["hwnd"],
                "additionalProperties": False,
            },
            handler=desktop_activate_window_handler,
            authority_level=AuthorityLevel.L2_EXECUTION,
            is_async=True,
            timeout_seconds=2.0,
        )
    )
    
    registry.register(
        ToolDefinition(
            name="presence.read_clipboard",
            description="Reads text from Windows clipboard. Requires explicit user consent.",
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=presence_read_clipboard_handler,
            authority_level=AuthorityLevel.L3_PROTECTED,
            is_async=True,
            timeout_seconds=1.0,
        )
    )
```

---

## 6. Threading Model, Timeouts & Failure Recovery

Windows desktop APIs can hang when communicating with unresponsive or deadlocked applications. The capability subsystem isolates all native Win32 calls from the main asyncio thread:

```
[Main asyncio Event Loop]
       │
       │ await anyio.to_thread.run_sync(handler, args)
       ▼
[AnyIO Thread Worker]
       │
       │ Native Win32 API call (with kernel timeout)
       ▼
[Windows OS / Target Application Window]
```

### 6.1 Failure & Timeout Protocol
1. **Thread Worker Isolation**: Any native call that can block on a window message (e.g. `SendMessage`, `SetForegroundWindow`, `ShellExecuteEx`) runs inside `anyio.to_thread.run_sync()`.
2. **Hard Timeouts**: Every capability execution is wrapped in an `anyio.fail_after(tool.timeout_seconds)` block. If the target window or shell hangs, the turn does not stall.
3. **Fail-Closed on Invalid HWND**: If a window is closed or destroyed between selection and activation, `IsWindow(hwnd)` returns `FALSE` and the capability returns an explicit structured error:
   ```json
   {
     "success": false,
     "error": "WINDOW_NOT_FOUND",
     "message": "Window handle 0x1A02E is no longer valid. The window may have been closed."
   }
   ```
4. **Zero State Mutation on Failure**: Failed capability invocations leave zero persistent handles, unhooked resources, or half-completed process trees.

---

## 7. Architectural Verification & Acceptance Criteria

Specification 43 is ready for implementation only when all following criteria are satisfied:

1. **ToolRegistry Compatibility**: All capabilities register through `ToolDefinition` without modifying the core `ToolRegistry` data structure.
2. **Authority Enforcement**: `L3_PROTECTED` capabilities reject execution without a valid HMAC-bound `ConsentToken`.
3. **Executable Blacklisting**: `shell.open_path` strictly prevents direct launching of executables or scripts without Job Object containment.
4. **Thread Hang Immunity**: Hung window simulations prove that calling `desktop.activate_window` against a frozen process times out cleanly within 2.0s without locking the Presence runtime.
5. **No Parallel Execution Loop**: Capability execution flows entirely through `AgentExecutor` and the verified tool-calling loop.
