# SPECIFICATION 41 — PHASE 2C: WINDOWS CONTEXT ARCHITECTURE (CORRECTED)
# Persistent Intelligent Presence — Windows Context & Environmental Awareness Subsystem

## 1. Executive Summary & Mission

Phase 2C defines how the Presence Runtime connects its verified intelligence backend to the active Windows 11 desktop environment.

Presence is **not** a conventional chatbot, a standalone agent framework, a launcher replacement, an operating system replacement, or a continuous surveillance tool. Its purpose is to maintain a lightweight, deterministic, and privacy-preserving persistent presence within Windows 11.

Prior to Phase 2C, Presence established:
- **Phase 0 & 1**: Runtime foundation, single-instance Win32 lifecycle, SQLite WAL persistence, append-only JSONL event journal, and trajectory management.
- **Phase 2A**: Gemini model integration, provider abstraction, and `AgentExecutor` turn loop.
- **Phase 2B & 2B.1**: Subprocess process containment via native Win32 Job Objects, suspended process launch, handle isolation, stdio pipe transport with bounded memory, and multi-tier permission enforcement.

This specification establishes the corrected architecture of the `runtime/context/` subsystem, its lifecycle, native Win32 API contracts, event-driven pipeline, exact FILETIME tick identity, named pipe endpoint security, reentrancy-safe hook architecture, and integration with `AgentExecutor`.

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
|              | WinEvent hooks    +-----------------------+                     v                  |
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

Presence establishes a strict 11-dimension taxonomy of desktop context:

| ID | Context Dimension | Acquisition Mechanism | Native Win32 API | Sensitivity Class | Acquisition Mode | Expiration Trigger | Default Persistence | Model Egress | MCP Egress |
|---|---|---|---|---|---|---|---|---|---|
| **D01** | Foreground HWND | WinEvent hook | `GetForegroundWindow` | `PUBLIC_CONTEXT` | Event-driven | Window change | None (transient) | Yes (metadata) | No |
| **D02** | Active App Name | Process query | `QueryFullProcessImageNameW` | `PUBLIC_CONTEXT` | Event-driven | Window change | None (transient) | Yes | No |
| **D03** | Window Caption | Top-level query | `GetWindowTextW` | `PERSONAL_CONTEXT` | Event-driven | Window change | Trajectory step only | Yes (sanitized) | No |
| **D04** | Process ID (PID) | Thread/process query | `GetWindowThreadProcessId` | `PUBLIC_CONTEXT` | Event-driven | Process exit | None (transient) | Yes (metadata) | No |
| **D05** | Executable Path | Query process image | `QueryFullProcessImageNameW` | `PUBLIC_CONTEXT` | Event-driven | Process exit | None (transient) | Yes (metadata) | No |
| **D06** | Window Class Name | Window query | `GetClassNameW` | `PUBLIC_CONTEXT` | Event-driven | Window change | None (transient) | Yes (metadata) | No |
| **D07** | Process Creation Ticks | Process times query | `GetProcessTimes` | `PUBLIC_CONTEXT` | Event-driven | Process exit | None (transient) | Internal (TOCTOU guard) | No |
| **D08** | Workspace / Folder | Application Provider | Local Authenticated Pipe | `PERSONAL_CONTEXT` | Request-driven | Session change | Trajectory step only | Yes (if enabled) | No |
| **D09** | User-Selected Text | Explicit User Tool | Targeted UI Automation | `SENSITIVE_CONTEXT` | User-initiated | Single turn | Trajectory step only | Explicit turn only | No |
| **D10** | System Resources | Win32 telemetry | `GlobalMemoryStatusEx` | `PUBLIC_CONTEXT` | Request-driven | 5s monotonic TTL | None (transient) | Yes | No |
| **D11** | User File Attachment | Presence CLI / UI | Explicit file path | `PERSONAL_CONTEXT` | User-initiated | Conversation | SQLite / ArtifactStore | Explicit turn only | Approved tools |

---

## 4. Native Windows API Specifications & Accurate Win32 Semantics

All native Win32 APIs used for context acquisition are documented with official Microsoft Learn references, exact thread affinity rules, and verified operational semantics:

### 4.1 Window Identification & Caption Retrieval

#### `GetForegroundWindow`
- **Documentation**: [Microsoft Learn — GetForegroundWindow](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getforegroundwindow)
- **Signature**: `HWND GetForegroundWindow(VOID)`
- **Behavior**: Retrieves a handle to the foreground window. Non-blocking.
- **Failure / Edge Cases**: Returns `NULL` if foreground window is changing, desktop is locked, UAC prompt is displayed, or screen saver is active.
- **Presence Policy**: Returns `None`; suppresses event publication if `HWND == NULL`.

#### `GetWindowTextW` (Technical Clarification — P0-01)
- **Documentation**: [Microsoft Learn — GetWindowTextW](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getwindowtextw)
- **Signature**: `int GetWindowTextW(HWND hWnd, LPWSTR lpString, int nMaxCount)`
- **Accurate Operational Semantics**:
  - *Calling Process Window*: Sends `WM_GETTEXT` message to the window procedure.
  - *Other Process Top-Level Window*: User32 retrieves the cached window caption directly from the internal window structure in the desktop heap. **It does NOT send `WM_GETTEXT` across processes.** Therefore, an unresponsive or hung external application cannot cause `GetWindowTextW` to block or deadlock during top-level window caption retrieval.
  - *Other Process Child Control*: `GetWindowTextW` **cannot** retrieve the text of a child control (e.g. edit box, document pane, web content) in another process.
- **Presence Implementation**:
  - Used strictly for top-level window captions.
  - Buffer allocation: Fixed buffer of 512 `WCHAR` (1024 bytes).
  - Truncation: Clamped to 200 characters in the adapter layer.
  - No synthetic message timeouts (`SendMessageTimeoutW`) are needed for top-level captions.

#### `GetClassNameW`
- **Documentation**: [Microsoft Learn — GetClassNameW](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getclassnamew)
- **Signature**: `int GetClassNameW(HWND hWnd, LPWSTR lpClassName, int nMaxCount)`
- **Behavior**: Retrieves the name of the window class from the internal class atom. Fast, non-blocking, does not send window messages. Buffer: 256 `WCHAR`.

#### `GetWindowThreadProcessId`
- **Documentation**: [Microsoft Learn — GetWindowThreadProcessId](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getwindowthreadprocessid)
- **Signature**: `DWORD GetWindowThreadProcessId(HWND hWnd, LPDWORD lpdwProcessId)`
- **Behavior**: Retrieves thread and process ID. Fast, direct memory read, non-blocking.

### 4.2 Process Identity, FILETIME Representation & TOCTOU Protection (P2C-18)

#### `GetProcessTimes` & Canonical FILETIME Ticks
- **Documentation**: [Microsoft Learn — GetProcessTimes](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getprocesstimes), [FILETIME Structure](https://learn.microsoft.com/en-us/windows/win32/api/minwinbase/ns-minwinbase-filetime)
- **FILETIME Definition**: A 64-bit integer representing the number of **100-nanosecond intervals** (ticks) since January 1, 1601 (UTC).
- **Canonical Representation**: To prevent precision loss and unit confusion, Presence stores and compares process creation timestamps strictly as a 64-bit integer tick count: `process_creation_filetime_ticks: int`.

```python
# Number of 100-nanosecond intervals between 1601-01-01 00:00:00 UTC and 1970-01-01 00:00:00 UTC
FILETIME_UNIX_EPOCH_OFFSET_TICKS = 116444736000000000

def filetime_to_ticks(high: int, low: int) -> int:
    """Constructs canonical 64-bit integer tick count from FILETIME high/low DWORDs."""
    return (high << 32) | low

def filetime_ticks_to_unix_ns(ticks: int) -> int:
    """Converts FILETIME 100-ns ticks to Unix epoch nanoseconds."""
    return (ticks - FILETIME_UNIX_EPOCH_OFFSET_TICKS) * 100

def filetime_ticks_to_unix_seconds(ticks: int) -> float:
    """Converts FILETIME 100-ns ticks to Unix epoch seconds (floating-point)."""
    return (ticks - FILETIME_UNIX_EPOCH_OFFSET_TICKS) / 10_000_000.0

def filetime_ticks_to_utc_datetime(ticks: int) -> datetime:
    """Converts FILETIME ticks to UTC datetime.
    Note: Python datetime has microsecond precision; sub-microsecond precision is truncated.
    """
    unix_ticks = ticks - FILETIME_UNIX_EPOCH_OFFSET_TICKS
    whole_microseconds = unix_ticks // 10
    return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=whole_microseconds)
```

> [!IMPORTANT]
> **Identity Comparison Invariant**: Canonical process identity validation **must always compare raw integer ticks** (`live_ticks == expected_ticks`). Conversions to float seconds or `datetime` are strictly for logging and human presentation.

---

## 5. Robust Window Identity Contract (P1-05, P2C-18)

HWND handles are assigned by the Windows window manager and are routinely recycled when windows close. Relying solely on `HWND` or `(HWND, PID)` creates a Time-of-Check to Time-of-Use (TOCTOU) vulnerability where an agent turn targets a window that was closed and whose handle was reassigned to a different application.

Presence establishes a composite, tamper-resistant **`WindowIdentity`** value object:

```python
@dataclass(frozen=True)
class WindowIdentity:
    """Strongly bound, composite identity of an observed Windows desktop window."""
    hwnd: int
    process_id: int
    process_creation_filetime_ticks: int   # Exact raw 100-ns FILETIME count
    window_class: str
    application_name: str
    observed_monotonic: float              # time.monotonic() tick
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
                live_ticks = (c_time.dwHighDateTime << 32) | c_time.dwLowDateTime
                return live_ticks == self.process_creation_filetime_ticks
            return False
        finally:
            ctypes.windll.kernel32.CloseHandle(h_proc)
```

### 5.1 TOCTOU Race Condition Boundaries
While composite verification drastically minimizes handle-recycling risks, no user-mode check can achieve a mathematical zero-nanosecond atomic guarantee across separate Win32 API calls. If a window closes in the microsecond between `matches_live_window()` and `SetForegroundWindow()`, `SetForegroundWindow()` safely returns `FALSE`, and Presence handles this gracefully via the taskbar flash fallback.

---

## 6. SetWinEventHook Lifecycle, Reentrancy & Resource Model (P2C-20)

### 6.1 Accurate WinEvent Architecture
- **Documentation**: [Microsoft Learn — SetWinEventHook](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwineventhook), [Guarding Against Reentrancy in Hook Functions](https://learn.microsoft.com/en-us/windows/win32/winauto/guarding-against-reentrancy-in-hook-functions)
- **Mechanism**: Windows delivers out-of-context accessibility events to the registered hook callback through the User32 WinEvent infrastructure.
- **Thread & Message Queue Ownership**:
  - The thread that calls `SetWinEventHook` **must run a Win32 message pump** (`GetMessageW` / `DispatchMessageW`).
  - Events are queued sequentially to this thread's message queue.
  - Dedicated background OS thread: `WinEventHookThread`.
  - When idle, `GetMessageW` sleeps inside a kernel wait; CPU usage is minimal, but Presence does not make misleading claims of "0.0% CPU" or "zero timer wakeups".

### 6.2 Minimal, Non-Blocking, Reentrancy-Safe Callback
Out-of-context hook callbacks can be reentered if message pumping occurs during execution. To guarantee safety and prevent thread stalls:
1. **Zero Expensive Work in Callback**: The callback does **NOT** query window text, inspect process images, allocate heavy structures, make COM calls, or perform disk I/O.
2. **Reentrancy Guard**: An atomic reentrancy counter immediately drops nested callback invocations.
3. **Bounded Thread-Safe Queue**: The callback places a compact tuple `(hwnd, time.monotonic())` into a thread-safe bounded queue (`collections.deque(maxlen=50)`).
4. **Handoff to AsyncIO**: Dispatches notification to the main asyncio loop via `loop.call_soon_threadsafe(manager._on_hook_event)`.

```python
class WinEventCallbackState:
    def __init__(self, loop, on_event_callback, max_queue=50):
        self.loop = loop
        self.on_event = on_event_callback
        self.queue = deque(maxlen=max_queue)
        self.reentrancy_lock = False

def native_winevent_proc(hWinEventHook, event, hwnd, idObject, idChild, idEventThread, dwmsEventTime):
    # Guard: only foreground events on top-level windows
    if event != 0x0003 or not hwnd or idObject != 0:
        return
        
    state = _GLOBAL_HOOK_STATE
    if state.reentrancy_lock:
        return  # Drop reentrant calls
        
    state.reentrancy_lock = True
    try:
        now_mono = time.monotonic()
        state.queue.append((hwnd, now_mono))
        state.loop.call_soon_threadsafe(state.on_event)
    finally:
        state.reentrancy_lock = False
```

### 6.3 Clean Unhooking & Teardown Protocol
During `ContextManager.stop()`:
1. Calls `UnhookWinEvent(hHook)` on the hook thread.
2. Posts `WM_QUIT` to the hook thread's message queue via `PostThreadMessageW()`.
3. Joins `WinEventHookThread` with a 500 ms timeout.
4. Clears all pending queue references, ensuring zero dangling callbacks or thread leaks across repeated cycles.

---

## 7. Authenticated Named Pipe Endpoint Architecture (P2C-19)

For Subphase 2C.5, Presence defines an authenticated local IPC channel to receive workspace metadata from developer tools (VS Code):

```
[Presence Runtime Server]
    │ (CreateNamedPipeW with explicit Logon SID DACL)
    │ (PIPE_REJECT_REMOTE_CLIENTS | FILE_FLAG_FIRST_PIPE_INSTANCE)
    ▼
[Named Pipe: \\.\pipe\presence-vscode-<logon-hash>-<session>]
    ▲
    │ (ConnectNamedPipe)
    │ 1. OS Client Verification (PID, Token, User SID, Executable Image)
    │ 2. Cryptographic Mutual Challenge-Response (HMAC-SHA256, Nonces)
    │ 3. Strict Framed JSON Message Protocol (Allowlisted Operations)
[VS Code Extension Client]
```

### 7.1 Transport Security & Server Hardening
- **Pipe Naming**: `\\.\pipe\presence-vscode-<logon-sid-hash>-<session-id>`. Contains no secrets; scoped to user session.
- **DACL Restriction**: Explicit Security Descriptor granting access strictly to:
  * Current User SID (`(A;;GRGW;;;<UserSID>)`)
  * Current Logon SID (`(A;;GRGW;;;<LogonSID>)`), preventing cross-session or service access.
- **Flags**:
  * `FILE_FLAG_FIRST_PIPE_INSTANCE` (`0x00080000`): Prevents pipe pre-creation / hijacking attacks.
  * `PIPE_REJECT_REMOTE_CLIENTS` (`0x00000008`): Strictly rejects network/SMB connections.

### 7.2 OS Peer Verification & Mutual Cryptographic Authentication
1. **OS Identity Verification**:
   - `GetNamedPipeClientProcessId(hPipe, &client_pid)`
   - `GetNamedPipeClientSessionId(hPipe, &client_session)`
   - Inspect client process via `OpenProcess` + `QueryFullProcessImageNameW`: verifies image matches approved `Code.exe`.
   - Inspect process creation ticks via `GetProcessTimes`.
   - Inspect client token via `OpenProcessToken`: verifies User SID and Logon SID match Presence server.
2. **One-Time Pairing**:
   - Explicit user-initiated command (`presence vscode-pair`).
   - Generates single-use 256-bit pairing secret (`token_urlsafe(32)`), TTL = 5 minutes.
   - Extension stores secret in VS Code `context.secrets` (`SecretStorage`).
   - Presence stores secret in user-scoped protected store.
3. **Mutual Challenge-Response**:
   - Presence issues fresh 256-bit `server_nonce`.
   - Extension replies with `client_nonce` and `HMAC-SHA256(shared_secret, "CLIENT_AUTH" || server_nonce || client_nonce || client_pid || creation_ticks)`.
   - Presence validates HMAC via `secrets.compare_digest()`, then returns `HMAC-SHA256(shared_secret, "SERVER_AUTH" || server_nonce || client_nonce)`.
   - Connection is bound to `(client_pid, process_creation_filetime_ticks, nonces)`.

### 7.3 Message Framing & Operation Allowlist
- **Framing**: Byte pipe with 4-byte big-endian length prefix (`uint32`), max frame 64 KB.
- **Strict Operation Allowlist**:
  * `provider.hello`
  * `provider.get_workspace_metadata` (workspace root path, open folder names, trust state)
  * `provider.get_active_editor_metadata` (relative file path, language ID; NO file content)
  * `provider.status`
  * `provider.shutdown`
- **Same-User Threat Boundary**: Documented clearly that DACL + Logon SID + Process Identity + HMAC protects against other users, remote attacks, non-paired applications, and cross-session snooping. It does not protect against malware executing arbitrary code inside the same user security context with full access to user memory.

---

## 8. Resource Management & Measurable Engineering Budgets (P1-03, P2C-20)

Resource values are explicit **Design Targets & Acceptance Ceilings**, to be empirically measured upon implementation:

| Resource Metric | Baseline (Phase 2B.1) | Phase 2C Design Target | Max Acceptance Ceiling | Measurement Tool |
|---|---|---|---|---|
| **Idle Working Set (WS)** | 64.5 MB | +3.5 MB | < 75.0 MB | Win32 `GetProcessMemoryInfo` |
| **Idle Private Bytes** | 50.2 MB | +2.0 MB | < 60.0 MB | Win32 `GetProcessMemoryInfo` |
| **OS Thread Count** | 6 threads | +1 thread (`WinEventHookThread`) | ≤ 8 threads | Toolhelp32 snapshot |
| **OS Handles (Idle)** | 249 handles | +4 handles (Hook, queue, proc) | < 270 handles | `psutil.Process().num_handles()` |
| **Idle CPU Utilization** | Low | < 0.05% (Event-driven kernel wait) | < 0.1% | `GetSystemTimes` over 5 min soak |
| **Event Debounce Latency** | N/A | 150 ms | 200 ms | `time.perf_counter()` |
| **Max Snapshot Size** | N/A | < 4 KB | < 16 KB | Recursive `sys.getsizeof` |

---

## 9. Architectural Verification & Acceptance Criteria

Specification 41 is ready for implementation only when all following criteria are satisfied:
1. **Canonical FILETIME Ticks**: Process creation identity uses exact 64-bit integer tick counts (`process_creation_filetime_ticks`).
2. **Reentrancy-Safe Hook**: WinEvent callback is minimal, guarded against reentrancy, and places compact records into a bounded queue without blocking.
3. **Realistic Resource Expectations**: Absolute claims of zero CPU/wakeups are eliminated; realistic event-driven budgets are defined with standardized soak test experiments.
4. **Hardened Named Pipe IPC**: VS Code named pipe architecture specifies explicit Logon SID DACL, client PID/token checks, mutual HMAC challenge-response, and 64 KB framed message bounds.
5. **Clean Unhooking**: Teardown guarantees `UnhookWinEvent`, `WM_QUIT`, and hook thread termination within 500 ms without handle leaks.
