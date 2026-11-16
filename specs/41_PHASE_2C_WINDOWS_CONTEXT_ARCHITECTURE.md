# SPECIFICATION 41 — PHASE 2C: WINDOWS CONTEXT ARCHITECTURE (CORRECTED)
# Persistent Intelligent Presence — Windows Context & Environmental Awareness Subsystem

## 1. Executive Summary & Mission

Phase 2C defines how the Presence Runtime connects its verified intelligence backend to the active Windows 11 desktop environment.

Presence is **not** a conventional chatbot, a standalone agent framework, a launcher replacement, an operating system replacement, or a continuous surveillance tool. Its purpose is to maintain a lightweight, deterministic, and privacy-preserving persistent presence within Windows 11.

Prior to Phase 2C, Presence established:
- **Phase 0 & 1**: Runtime foundation, single-instance Win32 lifecycle, SQLite WAL persistence, append-only JSONL event journal, and trajectory management.
- **Phase 2A**: Gemini model integration, provider abstraction, and `AgentExecutor` turn loop.
- **Phase 2B & 2B.1**: Subprocess process containment via native Win32 Job Objects, suspended process launch, handle isolation, stdio pipe transport with bounded memory, and multi-tier permission enforcement.

This corrected specification establishes the architecture of the `runtime/context/` subsystem, its lifecycle, native Win32 API contracts, event-driven pipeline, item-level sensitivity, and integration with `AgentExecutor`.

---

## 2. Integration Points with Existing Architecture

Phase 2C is strictly additive to the existing backend and preserves all core invariants:

```
+---------------------------------------------------------------------------------------------------+
|                                        PRESENCE RUNTIME                                           |
|                                                                                                   |
|  +------------------------+      +-----------------------+      +------------------------------+  |
|  |     EventBus           |<-----+    ContextManager     +----->|       AgentExecutor          |  |
|  |  (runtime/events.py)   |      |   (runtime/context/)  |      | (runtime/agent_executor.py)  |  |
|  +-----------+------------+      +-----------+-----------+      +--------------+---------------+  |
|              ^                               ^                                 |                  |
|              | publishes                     | updates snapshot                | invokes tools    |
|              |                               |                                 v                  |
|  +-----------+------------+      +-----------+-----------+      +------------------------------+  |
|  |     ContextSource      |      |    ContextSnapshot    |      |       ToolRegistry           |  |
|  | (ForegroundHookWorker) |      | (Immutable, Transient |      | (runtime/tools/registry.py)  |  |
|  +-----------+------------+      |   Single Point-in-    |      +--------------+---------------+  |
|              |                   |      Time Value)      |                     |                  |
|              | Win32 hooks       +-----------------------+                     v                  |
|              v                                                  +------------------------------+  |
|  +------------------------+                                     |     PermissionBoundary       |  |
|  |  Native Windows OS     |                                     |   (runtime/permissions.py)   |  |
|  |  (User32 / Kernel32)   |                                     +------------------------------+  |
+--+------------------------+--------------------------------------------------------------------+--+
```

1. **Native Platform Foundation (`runtime/platform_win32.py`)**:
   - Reuses existing Win32 handle management, error formatting via `FormatMessageW`, and single-instance mutex patterns.
   - Extends platform bindings to include native window inspection and event hook APIs using `ctypes.windll.user32` and `ctypes.windll.kernel32`.

2. **Event Bus (`runtime/events.py`)**:
   - Extends the core `EventBus` without introducing a secondary event system.
   - Context observations are dispatched as typed domain events (`WINDOW_FOREGROUND_CHANGED`, etc.) across the existing bus.

3. **Agent Executor (`runtime/agent_executor.py`)**:
   - Injects normalized, bounded `ContextSnapshot` instances into prompt assembly before LLM generation.
   - Treats all environmental text strictly as **untrusted data**, ensuring it cannot alter system instructions or bypass permissions.

4. **Persistence & Trajectory (`runtime/persistence.py`, `runtime/trajectory.py`)**:
   - Ambient environmental observations are **transient in-memory only**. They are **never** written to SQLite or JSONL simply because a window changed.
   - Only when a user-authorized turn executes a step that references environmental state, a minimal sanitized subset is captured in that step's metadata.

5. **Tool Registry & Permissions (`runtime/tools/registry.py`, `runtime/permissions.py`)**:
   - Windows contextual queries that require on-demand inspection register as standard tools with authority level `AuthorityLevel.L0_OBSERVE`.
   - Desktop execution actions register as `AuthorityLevel.L2_EXECUTE_REVERSIBLE` or `AuthorityLevel.L3_PROTECTED`.

---

## 3. Windows Context Dimensions & Authoritative Lifecycle

Presence establishes a strict 11-dimension taxonomy of desktop context. No context is collected without explicit classification:

| ID | Context Dimension | Acquisition Mechanism | Native Win32 API | Sensitivity Class | Acquisition Mode | Expiration Trigger | Default Persistence | Model Egress | MCP Egress |
|---|---|---|---|---|---|---|---|---|---|
| **D01** | Foreground HWND | WinEvent hook | `GetForegroundWindow` | `PUBLIC_CONTEXT` | Event-driven | Window change | None (transient) | Yes (metadata) | No |
| **D02** | Active App Name | Process query | `QueryFullProcessImageNameW` | `PUBLIC_CONTEXT` | Event-driven | Window change | None (transient) | Yes | No |
| **D03** | Window Caption | Top-level query | `GetWindowTextW` | `PERSONAL_CONTEXT` | Event-driven | Window change | Trajectory step only | Yes (sanitized) | No |
| **D04** | Process ID (PID) | Thread/process query | `GetWindowThreadProcessId` | `PUBLIC_CONTEXT` | Event-driven | Process exit | None (transient) | Yes (metadata) | No |
| **D05** | Executable Path | Query process image | `QueryFullProcessImageNameW` | `PUBLIC_CONTEXT` | Event-driven | Process exit | None (transient) | Yes (metadata) | No |
| **D06** | Window Class Name | Window query | `GetClassNameW` | `PUBLIC_CONTEXT` | Event-driven | Window change | None (transient) | Yes (metadata) | No |
| **D07** | Process Start Time | Process times query | `GetProcessTimes` | `PUBLIC_CONTEXT` | Event-driven | Process exit | None (transient) | Internal (TOCTOU guard) | No |
| **D08** | Workspace / Folder | Application Provider | Local IPC / Extension | `PERSONAL_CONTEXT` | Request-driven | Session change | Trajectory step only | Yes (if enabled) | No |
| **D09** | User-Selected Text | Explicit User Tool | Targeted UI Automation | `SENSITIVE_CONTEXT` | User-initiated | Single turn | Trajectory step only | Explicit turn only | No |
| **D10** | System Resources | Win32 telemetry | `GlobalMemoryStatusEx` | `PUBLIC_CONTEXT` | Request-driven | 5s monotonic TTL | None (transient) | Yes | No |
| **D11** | User File Attach | Presence CLI / UI | Explicit file path | `PERSONAL_CONTEXT` | User-initiated | Conversation | SQLite / ArtifactStore | Explicit turn only | Approved tools |

> [!IMPORTANT]
> **Clipboard Access Deferred**: Indiscriminate or ambient clipboard monitoring is strictly prohibited. Reading clipboard content is deferred from default Phase 2C capabilities. If implemented in a future milestone, it requires explicit user initiation and single-use L3 consent.

---

## 4. Native Windows API Specifications & Accurate Win32 Semantics

All native Win32 APIs used for context acquisition are documented with official Microsoft Learn references, exact thread affinity rules, and verified operational semantics:

### 4.1 Window Identification & Caption Retrieval

#### `GetForegroundWindow`
- **Documentation**: [Microsoft Learn — GetForegroundWindow](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getforegroundwindow)
- **Signature**: `HWND GetForegroundWindow(VOID)`
- **Behavior**: Retrieves a handle to the foreground window (the window with which the user is currently working).
- **Thread Affinity**: None. Callable from any thread.
- **Failure / Edge Cases**: Returns `NULL` if the foreground window is changing, the desktop is locked, a fullscreen UAC prompt is displayed, or a screen saver is running.
- **Presence Policy**: Returns `None`; suppresses event publication if `HWND == NULL`.

#### `GetWindowTextW` (Technical Correction — P0-01)
- **Documentation**: [Microsoft Learn — GetWindowTextW](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getwindowtextw)
- **Signature**: `int GetWindowTextW(HWND hWnd, LPWSTR lpString, int nMaxCount)`
- **Accurate Operational Semantics**:
  - *Calling Process Window*: Sends `WM_GETTEXT` message to the window procedure.
  - *Other Process Top-Level Window*: User32 retrieves the cached window caption directly from the internal window structure in the desktop heap. **It does NOT send `WM_GETTEXT` across processes.** Therefore, an unresponsive or hung external application cannot cause `GetWindowTextW` to block or deadlock during top-level window caption retrieval.
  - *Other Process Child Control*: `GetWindowTextW` **cannot** retrieve the text of a child control (e.g. edit box, document pane, web content) in another process.
- **Presence Implementation**:
  - Used strictly for top-level window captions.
  - Buffer allocation: Fixed buffer of 512 `WCHAR` (1024 bytes).
  - Truncation: Hard clamped to 200 characters in the adapter layer.
  - No synthetic message timeouts (`SendMessageTimeoutW`) are needed for top-level captions.

#### `GetClassNameW`
- **Documentation**: [Microsoft Learn — GetClassNameW](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getclassnamew)
- **Signature**: `int GetClassNameW(HWND hWnd, LPWSTR lpClassName, int nMaxCount)`
- **Behavior**: Retrieves the name of the window class from the internal class atom. Fast, non-blocking, does not send window messages.
- **Buffer**: 256 `WCHAR`.

#### `GetWindowThreadProcessId`
- **Documentation**: [Microsoft Learn — GetWindowThreadProcessId](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getwindowthreadprocessid)
- **Signature**: `DWORD GetWindowThreadProcessId(HWND hWnd, LPDWORD lpdwProcessId)`
- **Behavior**: Retrieves the identifier of the thread and process that created the window. Fast, direct memory read, non-blocking.

### 4.2 Process Identity & TOCTOU Protection

#### `OpenProcess` & `QueryFullProcessImageNameW`
- **Documentation**: [Microsoft Learn — OpenProcess](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-openprocess), [QueryFullProcessImageNameW](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-queryfullprocessimagenamew)
- **Access Mask**: `PROCESS_QUERY_LIMITED_INFORMATION` (`0x1000`). Never requests elevated rights.
- **Handle Lifetime**: Wrapped in strict `try/finally` with `CloseHandle()`.
- **Buffer**: 1024 `WCHAR`.

#### `GetProcessTimes` (Window Identity TOCTOU Guard — P1-05)
- **Documentation**: [Microsoft Learn — GetProcessTimes](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getprocesstimes)
- **Behavior**: Retrieves process creation, exit, kernel, and user times.
- **Purpose in Presence**: Captures `lpCreationTime` (`FILETIME` struct converted to a 64-bit integer). This timestamp uniquely identifies the process instance, preventing HWND and PID recycling attacks.

### 4.3 Event Hook Infrastructure (`SetWinEventHook`)

- **Documentation**: [Microsoft Learn — SetWinEventHook](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwineventhook)
- **Parameters**:
  - `eventMin` / `eventMax`: `EVENT_SYSTEM_FOREGROUND` (`0x0003`).
  - `hmodWinEventProc`: `NULL`.
  - `pfnWinEventProc`: Pointer to Python `WINEVENTPROC` callback via `ctypes`.
  - `idProcess` / `idThread`: `0` (global desktop hook).
  - `dwFlags`: `WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS` (`0x0000 | 0x0002`).
- **Hook Thread Architecture**:
  - Out-of-context hooks require an active Win32 message pump (`GetMessageW` / `DispatchMessageW`).
  - Dedicated background OS thread: `WinEventHookThread`.
  - Handoff to asyncio: The hook callback queues a lightweight tuple `(hwnd, event_time_monotonic)` and dispatches to the asyncio event loop using `loop.call_soon_threadsafe()`.
- **Unhooking & Clean Teardown**:
  - Calling `UnhookWinEvent(hHook)` on the hook thread during `ContextManager.stop()`.
  - Posting `WM_QUIT` to break the message loop.
  - Joining the thread with a 500 ms timeout.

---

## 5. Robust Window Identity Contract (P1-05)

HWND handles are assigned by the Windows window manager and are routinely recycled when windows close. Relying solely on `HWND` or `(HWND, PID)` creates a Time-of-Check to Time-of-Use (TOCTOU) vulnerability where an agent turn targets a window that was closed and whose handle was reassigned to a different application.

Presence establishes a composite, tamper-resistant **`WindowIdentity`** value object:

```python
@dataclass(frozen=True)
class WindowIdentity:
    """Strongly bound, composite identity of an observed Windows desktop window."""
    hwnd: int
    process_id: int
    process_creation_time_ns: int   # 64-bit timestamp from GetProcessTimes
    window_class: str
    application_name: str
    observed_monotonic: float       # time.monotonic() tick
    snapshot_id: str

    def matches_live_window(self) -> bool:
        """Validates that the physical window has not been recycled or closed."""
        # 1. Verify window exists
        if not ctypes.windll.user32.IsWindow(self.hwnd):
            return False
            
        # 2. Verify current owning PID
        current_pid = ctypes.wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(self.hwnd, ctypes.byref(current_pid))
        if current_pid.value != self.process_id:
            return False
            
        # 3. Verify process creation time matches original process instance
        h_proc = ctypes.windll.kernel32.OpenProcess(0x1000, False, current_pid.value)
        if not h_proc:
            return False
        try:
            c_time = ctypes.wintypes.FILETIME()
            e_time = ctypes.wintypes.FILETIME()
            k_time = ctypes.wintypes.FILETIME()
            u_time = ctypes.wintypes.FILETIME()
            if ctypes.windll.kernel32.GetProcessTimes(
                h_proc, ctypes.byref(c_time), ctypes.byref(e_time),
                ctypes.byref(k_time), ctypes.byref(u_time)
            ):
                live_ns = (c_time.dwHighDateTime << 32) | c_time.dwLowDateTime
                return live_ns == self.process_creation_time_ns
            return False
        finally:
            ctypes.windll.kernel32.CloseHandle(h_proc)
```

---

## 6. Context Manager Architecture & Deep Immutability (P1-04)

### 6.1 Logical Package Structure

```
runtime/context/
├── __init__.py           # Public exports (ContextManager, ContextSnapshot, WindowIdentity)
├── models.py             # Deeply immutable value contracts and sensitivity enums
├── contracts.py          # Abstract interfaces (ContextSource, ContextAdapter, Provider)
├── source.py             # Base ContextSource class and lifecycle state machine
├── adapter.py            # Normalization, sanitization, and item-level classification
├── manager.py            # ContextManager coordinator, debouncing, snapshot cache
├── snapshot.py           # Snapshot builder and prompt block formatting
├── subscriptions.py      # Topic-based filtered subscriber router
├── expiration.py         # Expiration, TTL, and cache-eviction policies
├── sources/
│   ├── __init__.py
│   └── foreground_window.py  # WinEventHookThread and User32 window observer
└── providers/
    └── __init__.py
```

### 6.2 Deep Immutability Contract

Python's `@dataclass(frozen=True)` does not prevent mutation of nested dictionaries or lists. To guarantee complete thread safety and prevent TOCTOU mutations between context capture and agent turn execution:

1. All internal payloads are recursively converted into immutable types (`tuple` for sequences, frozen key-value tuples or custom `FrozenMapping` for dictionaries).
2. All strings are clamped to predefined maximum lengths during construction.
3. Timestamps are split strictly:
   - `observed_utc`: ISO-8601 string for logging, audit, and user presentation.
   - `observed_monotonic`: Float from `time.monotonic()` for all internal TTL and expiration calculations (immune to NTP sync and system clock shifts).

```python
@dataclass(frozen=True)
class ContextItem:
    """Individually classified contextual item."""
    key: str
    value: Any                  # Must be primitive (str, int, float, bool) or tuple
    sensitivity: ContextSensitivity
    provenance: str
    observed_monotonic: float
    observed_utc: str

@dataclass(frozen=True)
class ContextSnapshot:
    """Point-in-time immutable snapshot of ambient Windows desktop state."""
    snapshot_id: str
    window_identity: Optional[WindowIdentity]
    caption: str
    items: Tuple[ContextItem, ...]
    captured_monotonic: float
    captured_utc: str
    ttl_seconds: float = 300.0

    def is_expired(self) -> bool:
        return (time.monotonic() - self.captured_monotonic) > self.ttl_seconds
```

---

## 7. Context Precedence & AgentExecutor Integration

Environmental context is injected into `AgentExecutor` turn prompts under a strict **precedence hierarchy**:

```
+-------------------------------------------------------------------------------+
| 1. System Constitution & Security Guardrails (Immutable Top Authority)        |
|    - Constitutional rules, tool calling schemas, security invariants          |
+-------------------------------------------------------------------------------+
| 2. Current User Message (Explicit User Intent)                                |
|    - The exact instructions entered by the user for this turn                 |
+-------------------------------------------------------------------------------+
| 3. Explicit User-Selected Content & Attachments                               |
|    - Text highlighted by user or files explicitly attached                    |
+-------------------------------------------------------------------------------+
| 4. Ambient Desktop Context (UNTRUSTED METADATA)                                |
|    - Current foreground app and window caption, clearly isolated              |
+-------------------------------------------------------------------------------+
| 5. Conversation Trajectory & Short-Term Continuity                            |
|    - Prior turn dialogue, tool inputs and execution results                   |
+-------------------------------------------------------------------------------+
| 6. Retrieved Historical Summaries & Long-Term Memory                          |
|    - FTS5 recalled summaries from previous sessions                           |
+-------------------------------------------------------------------------------+
```

### Subagent and Conversation Isolation Rules:
1. **Subagents**: When an agent spawns a subagent via `invoke_subagent`, the subagent receives an **empty** environmental context by default. It only observes environment context if explicitly passed in its role brief.
2. **Conversation Switching**: Switching active conversation IDs immediately clears any active cached snapshot. Ambient context is re-evaluated fresh for the new conversation turn.
3. **Workspace Lineage**: In workspace modes `share`, `branch`, or `inherit`, trajectory steps are tracked per lineage, but ambient desktop context belongs strictly to the physical desktop session and is never persisted across branches.

---

## 8. Resource Management & Measurable Engineering Budgets (P1-03)

Resource values are explicit **Design Targets & Acceptance Ceilings**, to be empirically measured upon implementation:

| Resource Metric | Baseline (Phase 2B.1) | Phase 2C Target Overhead | Max Acceptance Ceiling | Measurement Tool |
|---|---|---|---|---|
| **Idle Working Set (WS)** | 64.5 MB | +3.5 MB | < 75.0 MB | Win32 `GetProcessMemoryInfo` |
| **Idle Private Bytes** | 50.2 MB | +2.0 MB | < 60.0 MB | Win32 `GetProcessMemoryInfo` |
| **OS Thread Count** | 6 threads | +1 thread (`WinEventHookThread`) | ≤ 8 threads | Toolhelp32 snapshot |
| **OS Handles (Idle)** | 249 handles | +4 handles (Hook, queue, proc) | < 270 handles | `psutil.Process().num_handles()` |
| **Idle CPU Utilization** | 0.0% | 0.0% (Zero wakeups) | < 0.1% | `GetSystemTimes` over 60s |
| **Event Debounce Latency** | N/A | 150 ms | 200 ms | `time.perf_counter()` |
| **Max Snapshot Size** | N/A | < 4 KB | < 16 KB | Recursive `sys.getsizeof` |

---

## 9. Architectural Verification & Acceptance Criteria

Specification 41 is ready for implementation only when all following criteria are satisfied:
1. **GetWindowTextW Correctness**: Top-level window caption retrieval is implemented using `GetWindowTextW` without synthetic timeouts, reflecting true User32 behavior.
2. **Composite Window Identity**: All window targeting uses `WindowIdentity` validating HWND, PID, and process creation time.
3. **Deep Immutability**: All dataclasses enforce deep immutability through primitive/tuple constraints.
4. **Zero Ambient History**: Ambient context is transient and limited to the single active snapshot; no history ring buffer is retained.
5. **Clean Unhooking**: Teardown guarantees `UnhookWinEvent`, `WM_QUIT`, and hook thread termination within 500 ms.
