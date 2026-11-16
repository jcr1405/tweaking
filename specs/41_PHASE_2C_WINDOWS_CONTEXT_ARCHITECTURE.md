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
- **FILETIME Definition**: A 64-bit unsigned integer representing the number of **100-nanosecond intervals** (ticks) since January 1, 1601 (UTC).
- **Canonical Representation**: To prevent precision loss and unit confusion, Presence stores and compares process creation timestamps strictly as a 64-bit integer tick count: `process_creation_filetime_ticks: int`.

```python
from datetime import datetime, timedelta, timezone

# Number of 100-nanosecond intervals between 1601-01-01 00:00:00 UTC and 1970-01-01 00:00:00 UTC:
# (369 years * 365 days + 89 leap days) * 86,400 s/day * 10,000,000 ticks/s
FILETIME_UNIX_EPOCH_OFFSET_TICKS = 116444736000000000

def filetime_to_ticks(high: int, low: int) -> int:
    """Constructs canonical 64-bit integer tick count from FILETIME high/low DWORDs."""
    return (high << 32) | low

def filetime_ticks_to_unix_parts(ticks: int) -> tuple[int, int, int]:
    """Exact integer conversion relations without floating-point arithmetic.
    Returns: (unix_100ns, unix_ns, unix_seconds_integer)
    """
    unix_100ns = ticks - FILETIME_UNIX_EPOCH_OFFSET_TICKS
    unix_ns = unix_100ns * 100
    unix_seconds_integer = unix_100ns // 10_000_000
    return unix_100ns, unix_ns, unix_seconds_integer

def filetime_ticks_to_utc_datetime(ticks: int) -> datetime:
    """Converts FILETIME ticks to UTC datetime using pure integer arithmetic.
    Note: Python datetime has microsecond resolution (1,000 ns); sub-microsecond
    precision (unix_100ns % 10, representing 0-900 ns) is truncated.
    The raw ticks: int is never altered.
    """
    unix_100ns = ticks - FILETIME_UNIX_EPOCH_OFFSET_TICKS
    unix_microseconds = unix_100ns // 10
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return epoch + timedelta(microseconds=unix_microseconds)
```

> [!IMPORTANT]
> **Identity Comparison Invariant**: Canonical process identity validation **must always compare raw integer ticks** (`live_ticks == expected_ticks`). Window TOCTOU validation never uses `datetime`. Conversions to `datetime` are strictly for human display and audit logs, with documented microsecond truncation. Float timestamps are strictly prohibited from the canonical identity path.

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

## 6. SetWinEventHook Lifecycle, Reentrancy & Bounded Dispatch Model (P2C-20, P2C-24, P2C-25)

### 6.1 Accurate WinEvent Architecture & Message Loop
- **Documentation**: [Microsoft Learn — SetWinEventHook](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwineventhook), [Guarding Against Reentrancy in Hook Functions](https://learn.microsoft.com/en-us/windows/win32/winauto/guarding-against-reentrancy-in-hook-functions)
- **Mechanism**: Windows delivers out-of-context accessibility events to the registered hook callback through the User32 WinEvent infrastructure.
- **Thread & Message Queue Ownership**:
  - The thread that calls `SetWinEventHook` **must host a dedicated Win32 message pump** (`GetMessageW` / `TranslateMessage` / `DispatchMessageW`).
  - Events are dispatched sequentially to this thread's message queue.
  - Dedicated background OS thread: `WinEventHookThread`.
  - When idle, `GetMessageW` enters a Windows kernel wait state (`Executive/UserRequest`). CPU usage drops to near-zero (< 0.05% design target, < 0.20% acceptance ceiling), but Presence does not make misleading claims of "0.0% CPU" or "zero timer wakeups".

### 6.2 Minimal, Non-Blocking, Reentrancy-Safe Callback & Coalesced Dispatch
Out-of-context hook callbacks can be reentered if message pumping occurs during execution. Furthermore, scheduling an event-loop notification for every raw callback would create an **unbounded asyncio notification queue** if the asyncio loop is temporarily delayed.

To establish a strictly bounded, reentrancy-safe pipeline:
1. **Zero Expensive Work in Callback**: The callback does **NOT** query window captions, inspect process images, allocate memory dynamically, make COM calls, or perform disk I/O.
2. **Reentrancy Guard**: An atomic reentrancy flag (`_in_callback`) drops nested callback invocations immediately.
3. **Bounded Raw Queue**: The callback places a compact tuple `(hwnd, time.monotonic())` into a thread-safe bounded queue (`collections.deque(maxlen=50)`). Overflow drops the oldest event to preserve the latest foreground state.
4. **Single-Pending-Notification / Coalescing Mechanism (P2C-24)**:
   - Maintains an atomic/synchronized state flag: `_notification_pending: bool = False`.
   - When an event arrives:
     * Appends to `_raw_event_queue`. If queue was full (50 items), oldest item is evicted; `events_dropped` is incremented.
     * Checks `_notification_pending`:
       - If `_notification_pending` is `True`: increments `events_coalesced`; does **not** schedule another callback.
       - If `_notification_pending` is `False`: sets `_notification_pending = True`; increments `notification_callbacks_scheduled`; dispatches a single notification via `loop.call_soon_threadsafe(manager._on_hook_event)`.
5. **AsyncIO Consumer Draining**:
   - In `manager._on_hook_event`:
     * Increments `notification_callbacks_executed`.
     * Drains all queued items from `_raw_event_queue` under lock.
     * Resets `_notification_pending = False` under lock.
     * Takes the latest event, applies 150 ms debouncing, and triggers snapshot update.
6. **Bounded Invariant**: `pending_notification_count <= 1` at all times. The asyncio event loop never accumulates a backlog of notification callbacks.

```python
class WinEventCallbackState:
    def __init__(self, loop, on_event_callback, max_queue=50):
        self.loop = loop
        self.on_event = on_event_callback
        self.queue = deque(maxlen=max_queue)
        self.lock = threading.Lock()
        self.in_callback = False
        self.notification_pending = False
        # Instrumentation counters
        self.raw_events_received = 0
        self.events_coalesced = 0
        self.events_dropped = 0
        self.queue_high_water_mark = 0
        self.notification_callbacks_scheduled = 0
        self.notification_callbacks_executed = 0

def native_winevent_proc(hWinEventHook, event, hwnd, idObject, idChild, idEventThread, dwmsEventTime):
    # Guard: only foreground events on top-level windows
    if event != 0x0003 or not hwnd or idObject != 0:
        return
        
    state = _GLOBAL_HOOK_STATE
    if state.in_callback:
        return  # Drop reentrant calls
        
    state.in_callback = True
    try:
        now_mono = time.monotonic()
        with state.lock:
            state.raw_events_received += 1
            if len(state.queue) == state.queue.maxlen:
                state.events_dropped += 1
            state.queue.append((hwnd, now_mono))
            state.queue_high_water_mark = max(state.queue_high_water_mark, len(state.queue))
            
            if not state.notification_pending:
                state.notification_pending = True
                state.notification_callbacks_scheduled += 1
                state.loop.call_soon_threadsafe(state.on_event)
            else:
                state.events_coalesced += 1
    finally:
        state.in_callback = False
```

### 6.3 Owner-Controlled Unhooking & Teardown Protocol (P2C-25)
In Win32 User32 architecture, `UnhookWinEvent` must be executed by the thread that installed the hook (`WinEventHookThread`). Allowing the runtime thread to unhook directly introduces race conditions and undefined handle destruction.

The teardown sequence is strictly owner-controlled:
1. `ContextManager.stop()` requests shutdown of `ForegroundWindowSource`.
2. `ForegroundWindowSource` sets `_stopping = True` to halt event processing.
3. Runtime thread posts `WM_QUIT` to `WinEventHookThread` via `PostThreadMessageW(thread_id, WM_QUIT, 0, 0)`.
4. `WinEventHookThread`'s `GetMessageW` returns 0 on `WM_QUIT`.
5. `WinEventHookThread` calls `UnhookWinEvent(self._hHook)` for its own hook, clears `self._hHook = None`, and exits its thread run method.
6. The runtime thread waits for the hook thread via `thread.join(timeout=1.0)`.
7. **Timeout Reality & Verification**:
   - If `thread.is_alive()` is `False`: Clean termination is verified. Pending queue references are cleared.
   - If `thread.is_alive()` is `True`: **DO NOT claim successful cleanup!** Report `HookShutdownTimeoutError` and log diagnostic evidence (thread TID, handle count).

---

## 7. Authenticated Named Pipe Endpoint Architecture (P2C-19, P2C-21, P2C-22, P2C-26, P2C-27)

For Subphase 2C.5, Presence defines an authenticated local IPC channel to receive workspace metadata from developer tools (VS Code):

```
[Presence Runtime Server]
    │ (CreateNamedPipeW with Logon SID DACL, First-Instance Flag)
    │ (dwOpenMode: 0x40080003, dwPipeMode: 0x00000008)
    ▼
[Named Pipe: \\.\pipe\presence-vscode-<username>-<logon_session_id>]
    ▲
    │ (ConnectNamedPipe)
    │ 1. OS Client Verification (PID, Path to Code.exe, Creation Ticks, Logon SID)
    │ 2. Bound Cryptographic Mutual Challenge-Response (HMAC-SHA256, Nonces, Transcript)
    │ 3. Strict 4-Byte Framed JSON Protocol (Allowlisted Read-Only Operations)
[VS Code Extension Client]
```

### 7.1 Transport Security, DACL Hardening & Parameter Placement (P2C-21, P2C-22)
- **Pipe Naming**: `\\.\pipe\presence-vscode-<username>-<logon_session_id>`. Scoped to the interactive logon session.
- **Hardened DACL (P2C-21)**:
  * Security Descriptor string: `D:P(A;;GA;;;OW)(A;;0x12019b;;;<LogonSID>)`
  * Owner (Presence server) receives `GENERIC_ALL` (`GA` / `0x1F01FF`), granting creation, administration, and full control.
  * Connected client (VS Code) receives strictly specific data-exchange rights under its **Logon SID**:
    `0x0012019B` (`FILE_READ_DATA | FILE_WRITE_DATA | FILE_READ_EA | FILE_WRITE_EA | FILE_READ_ATTRIBUTES | FILE_WRITE_ATTRIBUTES | READ_CONTROL | SYNCHRONIZE`).
  * **Withholding Pipe-Instance Creation Authority**: `FILE_CREATE_PIPE_INSTANCE` (`FILE_APPEND_DATA`, `0x0004`) is **strictly withheld** from the client ACE. The client cannot create additional server instances of the pipe.
  * **Cross-Session Isolation**: The broad User SID grant (`(A;;GRGW;;;<UserSID>)`) is **completely removed**. Processes running under the same user account in different logon sessions (e.g. secondary desktop, SSH, or elevated RunAs) possess different Logon SIDs and are rejected by Windows kernel security (`STATUS_ACCESS_DENIED`).
  * `bInheritHandle = FALSE` in `SECURITY_ATTRIBUTES` prevents handle inheritance by child processes.
- **Accurate `CreateNamedPipeW` Parameter Placement (P2C-22)**:
  * `dwOpenMode`:
    `PIPE_ACCESS_DUPLEX (0x00000003) | FILE_FLAG_FIRST_PIPE_INSTANCE (0x00080000) | FILE_FLAG_OVERLAPPED (0x40000000)`
    - `FILE_FLAG_FIRST_PIPE_INSTANCE` (`0x00080000`) guarantees that Presence creates the pipe. If a squatting pipe already exists, `CreateNamedPipeW` fails immediately with `ERROR_ACCESS_DENIED`. The server fails closed.
  * `dwPipeMode`:
    `PIPE_TYPE_BYTE (0x00000000) | PIPE_READMODE_BYTE (0x00000000) | PIPE_REJECT_REMOTE_CLIENTS (0x00000008) | PIPE_WAIT (0x00000000)`
    - `PIPE_REJECT_REMOTE_CLIENTS` (`0x00000008`) strictly blocks SMB/network clients at the NPFS kernel driver level.
    - Byte mode (`PIPE_TYPE_BYTE`) provides robust cross-platform streaming compatibility with Node.js / libuv, avoiding message-mode edge cases.

### 7.2 OS Peer Verification & Transcript-Bound Mutual Authentication (P2C-26)
1. **OS Peer Identity Verification**:
   - `GetNamedPipeClientProcessId(hPipe, &client_pid)`
   - `GetNamedPipeClientSessionId(hPipe, &client_session)`
   - `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, client_pid)`
   - `QueryFullProcessImageNameW`: verifies image path matches authentic `Code.exe` (or `Code - Insiders.exe`) under approved installation paths (`%LOCALAPPDATA%\Programs\Microsoft VS Code` or `%PROGRAMFILES%\Microsoft VS Code`).
   - `GetProcessTimes`: captures `client_creation_filetime_ticks: int` to guard against PID recycling.
   - `OpenProcessToken` + `GetTokenInformation(TokenGroups)`: asserts client token contains server's Logon SID.
2. **Short-Lived Pairing Flow**:
   - Explicit user-mediated command: `presence vscode-pair`.
   - Generates a 256-bit pairing secret (`secrets.token_hex(32)`), TTL = 5 minutes, single-use.
   - Secret is entered directly by the user into the VS Code extension UI; stored in VS Code `SecretStorage` (backed by Windows DPAPI / Credential Manager).
   - In Presence, stored in user-scoped DPAPI vault.
   - **Never** passed through command-line flags, environment variables, shell history, persistent logs, or unencrypted temp files.
3. **Bound Mutual HMAC-SHA256 Challenge-Response**:
   - Server sends `AUTH_CHALLENGE`: 32-byte cryptographically secure random `server_nonce` (hex).
   - Client generates 32-byte `client_nonce` (hex).
   - **Transcript Authentication**: Both parties construct the canonical transcript string:
     `T = f"2.0|{pipe_endpoint}|{server_pid}|{client_pid}|{client_creation_filetime_ticks}|{client_logon_sid}|{server_nonce}|{client_nonce}"`
   - Client sends `AUTH_RESPONSE`:
     `client_mac = HMAC-SHA256(pairing_key, b"PRESENCE_VSCODE_CLIENT_AUTH_V1|" + T.encode())`
   - Server verifies `client_mac` using constant-time comparison (`secrets.compare_digest`).
   - Server returns `AUTH_CONFIRM`:
     `server_mac = HMAC-SHA256(pairing_key, b"PRESENCE_VSCODE_SERVER_AUTH_V1|" + T.encode())`
   - Client independently verifies `server_mac` using `secrets.compare_digest`.
   - Ephemeral session key is derived:
     `session_key = HMAC-SHA256(pairing_key, b"PRESENCE_VSCODE_SESSION_KEY_V1|" + T.encode())`
   - Nonces expire after 10 seconds; single-use cache prevents replay attacks.
   - Maximum 1 active client connection (`nMaxInstances = 1`). Handshake timeout 500 ms; disconnects immediately on verification failure.

### 7.3 Message Framing & Canonical Operation Allowlist (P2C-27)
- **Framing**: Byte stream with 4-byte big-endian unsigned integer length prefix (`>I`), max frame 64 KB (`65536` bytes). Payloads exceeding 64 KB cause immediate connection termination.
- **Canonical Operation Allowlist (`vscode.*`)**:
  * `vscode.ping`: Health check and keep-alive (`{"status": "ok", "timestamp_utc": str}`).
  * `vscode.get_active_workspace`: Returns active workspace metadata (`{"workspace_name": str, "workspace_folders": list[str], "is_trusted": bool}`).
  * `vscode.get_active_editor_metadata`: Returns active document metadata (`{"file_path": str, "relative_path": str, "language_id": str, "is_dirty": bool}`).
  * `vscode.get_open_documents`: Returns list of open editor file paths (`{"open_documents": list[str]}`).
- **Strict Invariants**:
  * `provider.shutdown` is **REMOVED AND PROHIBITED**. Neither party can shut down or mutate the other via this channel.
  * Read-only metadata only: **Zero file contents**, zero code execution, zero keystrokes.
  * Ambient context is **never** sent to VS Code (unidirectional flow: VS Code metadata $\to$ Presence context).
  * No consent tokens can be minted or altered by the extension.
- **Same-User Threat Boundary**:
  Logon SID DACL + Image Verification + Creation Ticks + Transcript HMAC prevents network attackers, other Windows users, cross-session processes, pipe squatting, and un-paired local scripts. It does not defend against arbitrary malware executing inside the same user security context that has direct access to process memory (`OpenProcess(PROCESS_VM_READ)`). This boundary is explicitly acknowledged.

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
1. **Canonical FILETIME Ticks**: Process creation identity uses exact 64-bit integer tick counts (`process_creation_filetime_ticks`). Datetime conversion uses pure integer arithmetic with documented microsecond truncation; zero float conversion in canonical identity path.
2. **Reentrancy-Safe Hook & Bounded Coalesced Dispatch**: WinEvent callback is minimal, guarded against reentrancy, and places compact records into a bounded queue (`maxlen=50`). Single-pending-notification mechanism ensures `pending_notification_count <= 1` at all times, preventing unbounded asyncio callback accumulation.
3. **Realistic Resource Expectations**: Absolute claims of zero CPU/wakeups are eliminated; realistic event-driven budgets are defined with standardized soak test experiments and explicit Design Targets vs Acceptance Ceilings.
4. **Hardened Named Pipe IPC**: VS Code named pipe architecture specifies explicit Logon SID DACL without User SID grant, strictly withholds `FILE_CREATE_PIPE_INSTANCE`, separates `dwOpenMode` from `dwPipeMode`, binds complete handshake transcript into mutual HMAC-SHA256 proofs, and restricts operations to canonical `vscode.*` metadata methods (no `provider.shutdown`).
5. **Owner-Controlled Unhooking**: Teardown guarantees `UnhookWinEvent` is executed strictly by `WinEventHookThread`. If `thread.join(timeout=1.0)` times out, an incomplete shutdown is reported rather than claiming clean termination.
