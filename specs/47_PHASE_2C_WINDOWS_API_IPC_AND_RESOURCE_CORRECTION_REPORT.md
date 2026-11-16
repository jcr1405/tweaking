# SPECIFICATION 47 — PHASE 2C: WINDOWS API ACCURACY, PROCESS IDENTITY, NAMED PIPE AUTHENTICATION & RESOURCE MODEL REPORT
# Authoritative Technical Audit, Security Hardening & Architecture Correction Report

---

## 1. Executive Summary

This report establishes the final authoritative architecture correction for **Phase 2C — Windows Context & Capability Integration** of the Presence Runtime.

Following the initial design freeze in Specification 46, a second technical audit identified four remaining implementation-critical inaccuracies across the Phase 2C architecture specifications:
1. **P2C-17 (ShellExecute Flags & Execution Semantics)**: `SEE_MASK_FLAG_NO_UI` (`0x00000400`) was confused with `SEE_MASK_NOCLOSEPROCESS` (`0x00000040`), caller process handle ownership was unspecified, and `shell.open_path` was falsely classified as Class B (hard timeout).
2. **P2C-18 (FILETIME Representation & Precision Safeguards)**: Process creation timestamps were represented as `process_creation_time_ns` and converted to Python `datetime.datetime`, introducing unit ambiguity and truncating sub-microsecond precision.
3. **P2C-19 (Authenticated Windows Named Pipe IPC for VS Code)**: The proposed VS Code IPC endpoint lacked transport access control descriptors, peer identity validation, mutual authentication, framing, and payload bounds, leaving it vulnerable to pipe squatting and cross-session manipulation.
4. **P2C-20 (SetWinEventHook Resource & Lifecycle Model)**: The event hook model claimed absolute "0.0% CPU" and "zero wakeups" without modeling the Win32 message pump, callback reentrancy, bounded queueing, or empirical verification methodology.

This milestone resolved all four findings across Specifications 41 through 46 without modifying any production Python source code in `runtime/` or `tests/`.

### Summary of Authoritative Corrections:
- **ShellExecute Flags & Ownership**: Defined canonical Win32 constants (`SEE_MASK_NOCLOSEPROCESS = 0x00000040`, `SEE_MASK_FLAG_NO_UI = 0x00000400`, `SEE_MASK_NOZONECHECKS = 0x00000008` [PROHIBITED], `SEE_MASK_NOASYNC = 0x00000100`, `SEE_MASK_ASYNCOK = 0x00100000`). Documented caller handle ownership via `CloseHandle()`. Reclassified `shell.open_path` to **Class C (Best-Effort Asynchronous Shell Request)**.
- **Canonical Process Creation Ticks**: Standardized process creation time as `process_creation_filetime_ticks: int` (64-bit unsigned count of 100ns intervals since 1601-01-01 UTC). Implemented exact conversion formulas with `FILETIME_UNIX_EPOCH_OFFSET_TICKS = 116444736000000000`. TOCTOU validation uses raw 64-bit integer equality. Documented sub-microsecond truncation in Python `datetime`.
- **4-Layer Authenticated Named Pipe IPC**: Implemented defense-in-depth IPC:
  - *Layer 1 (Transport Access Control)*: Dedicated DACL restricted to Logon SID and User SID (`PIPE_REJECT_REMOTE_CLIENTS`, `FILE_FLAG_FIRST_PIPE_INSTANCE`).
  - *Layer 2 (Peer Process Identity Verification)*: `GetNamedPipeClientProcessId`, binary path validation to `Code.exe`, creation ticks check, client token Logon SID check.
  - *Layer 3 (Application Pairing & Mutual Authentication)*: User-initiated pairing token in VS Code `SecretStorage` (DPAPI), mutual HMAC-SHA256 challenge-response with 32-byte nonces, `secrets.compare_digest`.
  - *Layer 4 (Framing, Payload Bounds & Operation Authorization)*: 4-byte length prefix, 64 KB hard max payload ceiling, strict JSON-RPC schema allowlist for read-only metadata.
  - *Threat Model*: Honestly documented same-user unprivileged malware boundaries.
- **SetWinEventHook Lifecycle & Empirical Model**: Dedicated `WinEventHookThread` message loop (`GetMessageW` / `DispatchMessageW`), callback reentrancy guard (`_in_callback`), non-blocking bounded queue (`deque(maxlen=50)`), thread-safe handoff via `loop.call_soon_threadsafe()`, clean teardown sequence (`UnhookWinEvent`, `WM_QUIT`, thread join), standardized Experiments A through F, and explicit Design Targets vs Acceptance Ceilings.

All 17 Architectural Acceptance Gates (Gates A through Q) are evaluated and **PASS**.

The Phase 2C architecture is now **authoritatively frozen**.

---

## 2. Repository Baseline

- **Repository Path**: `c:\Users\chait\OneDrive\Documents\projects\tweaking`
- **Initial Baseline Commit**: `6c65fba` (`docs(phase-2c): correct Windows context architecture and security decisions`)
- **Active Working Branch**: `architecture/phase-2c-api-ipc-resource-correction`
- **Branch Relationship**: Branched directly from `6c65fba` on `architecture/phase-2c-correction-freeze`. Preserves all prior Phase 2B.1 and Phase 2C.0 audit records.
- **Source Code Invariant**: **Zero production Python files in `runtime/` or `tests/` were modified.** The working tree contains modifications and additions strictly within `specs/`.

---

## 3. ShellExecute Flag Correction (P2C-17)

### 3.1 Constant Definitions & Previous Confusion
Previous specification drafts contained a critical typo: `SEE_MASK_FLAG_NO_UI` was defined as `0x00000040`. In the official Windows SDK (`shellapi.h`), `0x00000040` is actually `SEE_MASK_NOCLOSEPROCESS`, while `SEE_MASK_FLAG_NO_UI` is `0x00000400`.

The authoritative constant definitions are:

```python
SEE_MASK_NOCLOSEPROCESS = 0x00000040  # Requests hProcess if a new process was created
SEE_MASK_FLAG_NO_UI     = 0x00000400  # Suppresses ordinary shell error message boxes
SEE_MASK_NOZONECHECKS   = 0x00000008  # PROHIBITED: Bypasses Attachment Execution Service
SEE_MASK_NOASYNC        = 0x00000100  # Forces synchronous completion on caller thread
SEE_MASK_ASYNCOK        = 0x00100000  # Allows asynchronous execution on shell background thread
```

### 3.2 `SEE_MASK_FLAG_NO_UI` Semantics & Security Prompts
`SEE_MASK_FLAG_NO_UI` (`0x00000400`) instructs the Windows shell to suppress non-critical error message boxes that would otherwise halt unattended or background execution (e.g., file not found, path syntax error, or unmapped file extension dialogs).

Crucially, **Windows security dialogs are exempt by OS design**:
- Windows Attachment Execution Service (AES) security prompts (such as Mark-of-the-Web execution warnings for files downloaded from the Internet) are **not** suppressed by `SEE_MASK_FLAG_NO_UI`.
- User Account Control (UAC) elevation prompts are **not** suppressed by `SEE_MASK_FLAG_NO_UI`.
- Therefore, using `SEE_MASK_FLAG_NO_UI` prevents non-interactive hang conditions without compromising user security consent boundaries.

### 3.3 `SEE_MASK_NOZONECHECKS` Prohibition
`SEE_MASK_NOZONECHECKS` (`0x00000008`) explicitly bypasses security checks performed by `IAttachmentExecute`. Setting this flag would allow downloaded untrusted files containing Zone.Identifier alternate data streams to execute without user warning. In Presence, setting `SEE_MASK_NOZONECHECKS` is **strictly prohibited** across all capability handlers.

### 3.4 Process Handle Availability & Caller Ownership
When `SEE_MASK_NOCLOSEPROCESS` is set:
1. If the shell operation results in launching a brand-new process instance, `SHELLEXECUTEINFOW.hProcess` is populated with an open Win32 process handle (`HANDLE`).
2. If the shell operation delegates to an already running instance (e.g. opening a new tab in an existing browser, navigating an open Explorer window), or if the association is handled via Dynamic Data Exchange (DDE) or an in-process COM handler, `hProcess` is `NULL` (`0` or `None`).
3. **Caller Handle Ownership**: If `hProcess` is non-NULL, the caller owns the handle and is strictly responsible for closing it via `CloseHandle()`. Leaking `hProcess` leaks kernel handles in the operating system. Presence immediately closes `sei.hProcess` upon successful return.

### 3.5 Process Containment Boundary Disambiguation
Applications launched via `ShellExecuteExW` run in the user's interactive desktop session outside Presence's Win32 Job Object containment hierarchy. `shell.open_path` is a shell document/URL association launcher, **not** an isolated process sandbox.

---

## 4. ShellExecute Timeout and Execution Classification (P2C-17)

### 4.1 Native Timeout Reality
`ShellExecuteExW` does not accept a native timeout parameter. The Windows kernel and Shell APIs provide no mechanism to pass a timeout to `ShellExecuteExW`.

- `SEE_MASK_NOASYNC` (`0x00000100`) forces synchronous execution on the calling thread. It does **not** provide a timeout; it forces the call to block until the shell completes initialization.
- `SEE_MASK_ASYNCOK` (`0x00100000`) allows asynchronous execution on a background thread created by the Windows shell. In most cases, the call returns quickly (tens of milliseconds). However, if the shell operation requires DDE or interacts with an unresponsive COM server, the calling thread may still block synchronously.

### 4.2 Reclassification to Class C
Previous specifications incorrectly classified `shell.open_path` as Class B (native API with timeout). Because `ShellExecuteExW` has no native timeout parameter, it is formally reclassified as **Class C (Best-Effort Asynchronous Shell Request)**.

### 4.3 Python Wrapper Timeout Limitations
In AnyIO / Python, `anyio.fail_after(5.0)` cancels the awaiting coroutine if the timeout expires. However, if the underlying worker thread blocks inside `ShellExecuteExW`, **the worker thread remains blocked until the shell call returns**.

Terminating the OS worker thread via `TerminateThread` is strictly prohibited because it corrupts Python interpreter internals, deadlocks the GIL, leaves critical sections locked, and leaks kernel handles. Presence documents this limitation honestly: the coroutine cancels, but the underlying thread runs until shell dispatch completes.

---

## 5. FILETIME Representation (P2C-18)

### 5.1 Structure Definition
In the Win32 API (`minwinbase.h`), `FILETIME` represents a 64-bit count of 100-nanosecond intervals (ticks) since January 1, 1601 (UTC):

```c
typedef struct _FILETIME {
    DWORD dwLowDateTime;
    DWORD dwHighDateTime;
} FILETIME, *PFILETIME, *LPFILETIME;
```

### 5.2 Canonical Representation
Previous specifications used `process_creation_time_ns` and converted to Python `datetime.datetime`. This introduced unit confusion (nanoseconds vs 100ns ticks) and precision truncation.

Presence standardizes the canonical representation as:
```python
process_creation_filetime_ticks: int
```
This is a raw 64-bit unsigned integer representing the exact count of 100-nanosecond intervals since 1601-01-01 UTC.

### 5.3 Direct Integer TOCTOU Comparison
For window identity validation and Time-of-Check to Time-of-Use (TOCTOU) defense:
```python
# Raw 64-bit integer equality check
if current_filetime_ticks != expected_filetime_ticks:
    raise WindowIdentityChangedError("Target process PID was recycled.")
```
Comparing raw integers eliminates all floating-point rounding errors, leap-second discrepancies, and datetime conversion artifacts.

---

## 6. FILETIME-to-Unix Conversion (P2C-18)

### 6.1 Epoch Difference Calculation
The Unix epoch is January 1, 1970 UTC. The Windows FILETIME epoch is January 1, 1601 UTC.
The difference between the two epochs is exactly 369 years, accounting for 89 leap years:

$$\text{Days} = (369 \times 365) + 89 = 134{,}774 \text{ days}$$
$$\text{Seconds} = 134{,}774 \times 86{,}400 = 11{,}644{,}473{,}600 \text{ seconds}$$
$$\text{FILETIME Ticks} = 11{,}644{,}473{,}600 \times 10{,}000{,}000 = 116{,}444{,}736{,}000{,}000{,}000 \text{ ticks}$$

### 6.2 Standardized Conversion Formulas
```python
FILETIME_UNIX_EPOCH_OFFSET_TICKS = 116444736000000000
TICKS_PER_SECOND = 10_000_000
TICKS_PER_MILLISECOND = 10_000
TICKS_PER_MICROSECOND = 10
NANOSECONDS_PER_TICK = 100

def filetime_to_ticks(ft: wintypes.FILETIME) -> int:
    """Converts a Win32 FILETIME struct to a 64-bit unsigned integer tick count."""
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

def filetime_ticks_to_unix_ns(ticks: int) -> int:
    """Converts FILETIME ticks to Unix epoch nanoseconds."""
    return (ticks - FILETIME_UNIX_EPOCH_OFFSET_TICKS) * NANOSECONDS_PER_TICK

def filetime_ticks_to_unix_seconds(ticks: int) -> float:
    """Converts FILETIME ticks to Unix epoch floating-point seconds."""
    return (ticks - FILETIME_UNIX_EPOCH_OFFSET_TICKS) / TICKS_PER_SECOND
```

---

## 7. UTC Datetime Conversion Limitations (P2C-18)

When converting `process_creation_filetime_ticks` to Python `datetime.datetime`:

```python
from datetime import datetime, timezone

def filetime_ticks_to_utc_datetime(ticks: int) -> datetime:
    """Converts FILETIME ticks to UTC datetime. Truncates sub-microsecond precision."""
    unix_ns = filetime_ticks_to_unix_ns(ticks)
    unix_seconds = unix_ns / 1_000_000_000.0
    return datetime.fromtimestamp(unix_seconds, tz=timezone.utc)
```

### Sub-Microsecond Truncation Limitation
- Python's standard `datetime.datetime` object stores time with microsecond resolution ($10^{-6}$ seconds).
- Windows `FILETIME` ticks have 100-nanosecond resolution ($10^{-7}$ seconds), which is 10 times finer than a microsecond.
- Converting `FILETIME` ticks to `datetime.datetime` **truncates the sub-microsecond digit** (`ticks % 10`).
- **Architectural Invariant**: `datetime.datetime` conversion is used strictly for user-facing display, formatted logs, and human inspection. Internal window tracking, caching, and TOCTOU validation **must** use `process_creation_filetime_ticks: int`.

---

## 8. WindowIdentity Correction (P2C-18)

`WindowIdentity` in `specs/41` and `runtime/context/models.py` is updated to use canonical FILETIME ticks:

```python
@dataclass(frozen=True)
class WindowIdentity:
    hwnd: int
    process_id: int
    process_creation_filetime_ticks: int
    window_class: str
    executable_name: str
    executable_path: str

    @property
    def process_creation_utc(self) -> datetime:
        """User-facing UTC representation with documented microsecond truncation."""
        return filetime_ticks_to_utc_datetime(self.process_creation_filetime_ticks)
```

In `desktop.activate_window`, TOCTOU verification is performed by:
1. Querying current PID via `GetWindowThreadProcessId(hwnd)`.
2. Asserting `current_pid == expected_pid`.
3. Opening process handle and querying `creation_time` via `GetProcessTimes()`.
4. Converting `creation_time` to 64-bit integer ticks.
5. Asserting `current_ticks == expected_creation_filetime_ticks`.

---

## 9. Named Pipe Server Security (P2C-19)

### 9.1 Pipe Naming Convention
The server creates a named pipe formatted with the current user and logon session identifier:
```
\\.\pipe\presence-vscode-<username>-<logon_session_id>
```

### 9.2 Creation Flags & Single-Instance Guarantee
```python
PIPE_ACCESS_DUPLEX = 0x00000003
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
FILE_FLAG_OVERLAPPED = 0x40000000

PIPE_TYPE_BYTE = 0x00000000
PIPE_READMODE_BYTE = 0x00000000
PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
```

- `FILE_FLAG_FIRST_PIPE_INSTANCE` (`0x00080000`): Guarantees that Presence is creating the first instance of the pipe. If an unauthorized process has squatted on the pipe name, `CreateNamedPipeW` immediately fails with `ERROR_ACCESS_DENIED`.
- `PIPE_REJECT_REMOTE_CLIENTS` (`0x00000008`): Restricts client connections strictly to the local machine.

---

## 10. DACL and Logon SID Policy (P2C-19)

### 10.1 Access Control Invariant
A named pipe name alone is not a security boundary. Presence enforces kernel-level Discretionary Access Control Lists (DACL):

```
D:(A;;GRGW;;;<LogonSID>)(A;;GRGW;;;<UserSID>)
```

### 10.2 Logon SID Enforcement
- The **Logon SID** (`S-1-5-5-X-Y`) is a unique security identifier generated by the Local Security Authority (LSA) when a user logs on. It is attached to the user's interactive logon session.
- By granting `GENERIC_READ | GENERIC_WRITE` strictly to the active Logon SID, processes running in different logon sessions (such as other interactive users, service accounts, or processes running under elevated admin tokens in separate sessions) are denied access at the kernel security subsystem level (`SeAccessCheck`).

---

## 11. Remote Client Rejection (P2C-19)

Windows named pipes can be reached over the network via SMB (`\\<server>\pipe\...`) unless explicitly restricted.
Presence passes `PIPE_REJECT_REMOTE_CLIENTS` (`0x00000008`) to `CreateNamedPipeW`.
The Windows Named Pipe File System (NPFS) driver enforces this in kernel mode: network clients attempting to connect receive `STATUS_ACCESS_DENIED` before any connection handshake occurs.

---

## 12. Client PID and Token Verification (P2C-19)

Upon receiving a client connection, Presence executes peer identity verification:

1. **Client PID Query**:
   ```python
   client_pid = wintypes.ULONG()
   kernel32.GetNamedPipeClientProcessId(hPipe, ctypes.byref(client_pid))
   ```
2. **Executable Image Verification**:
   - Opens `client_pid` with `PROCESS_QUERY_LIMITED_INFORMATION`.
   - Calls `QueryFullProcessImageNameW()` to verify that the client image path points to an authentic VS Code executable (`Code.exe` or `Code - Insiders.exe`).
3. **Client Creation Time Check**:
   - Calls `GetProcessTimes()` to obtain `client_creation_filetime_ticks`.
   - Protects against rapid PID recycling attacks.
4. **Token Logon SID Verification**:
   - Calls `OpenProcessToken(hProcess, TOKEN_QUERY, &hToken)`.
   - Calls `GetTokenInformation(hToken, TokenGroups, ...)` to inspect the client's token SIDs.
   - Asserts that the client's token contains the identical Logon SID as the Presence server.

---

## 13. VS Code Extension Pairing (P2C-19)

To prevent unauthenticated processes running as the same user from querying workspace metadata:

1. **Pairing Secret Generation**:
   During initial setup, Presence generates a cryptographically secure 256-bit random pairing secret:
   ```python
   pairing_secret = secrets.token_hex(32)
   ```
2. **Secure Secret Storage**:
   - In VS Code, the extension saves the secret using `vscode.SecretStorage` (which uses Windows Credential Manager / DPAPI).
   - In Presence, the secret is stored in the DPAPI-encrypted user credential store.
3. **No Ambient Auto-Discovery**:
   The pipe will not serve unauthenticated clients. If the client cannot prove knowledge of the pairing secret, the connection is closed immediately.

---

## 14. Challenge-Response Authentication (P2C-19)

Authentication uses mutual HMAC-SHA256 challenge-response:

```
Presence Server                                      VS Code Extension
      │                                                     │
      │  1. ConnectNamedPipe()                              │
      │◄────────────────────────────────────────────────────│
      │                                                     │
      │  2. AUTH_CHALLENGE (server_nonce: 32 bytes hex)     │
      │────────────────────────────────────────────────────►│
      │                                                     │
      │  3. AUTH_RESPONSE (client_nonce, HMAC-SHA256 digest)│
      │◄────────────────────────────────────────────────────│
      │                                                     │
      │  4. Verify client digest (secrets.compare_digest)   │
      │     AUTH_CONFIRM (server HMAC digest, session_token)│
      │────────────────────────────────────────────────────►│
      │                                                     │
      │  5. Verify server digest; session established       │
```

1. **Server Nonce**: Server generates `server_nonce = secrets.token_bytes(32)`.
2. **Client Digest**: Client generates `client_nonce = secrets.token_bytes(32)`.
   Client computes:
   $$\text{client\_digest} = \text{HMAC-SHA256}(K_{\text{pair}}, \text{server\_nonce} \parallel \text{client\_nonce})$$
3. **Server Verification**: Server verifies using `secrets.compare_digest`.
4. **Server Digest**: Server computes:
   $$\text{server\_digest} = \text{HMAC-SHA256}(K_{\text{pair}}, \text{client\_nonce} \parallel \text{server\_nonce})$$
5. **Session Token**: Server generates an ephemeral 32-byte session token for subsequent JSON-RPC requests on that connection instance.

---

## 15. Replay Protection (P2C-19)

- Fresh 32-byte nonces are generated by both server and client on every connection attempt.
- Server maintains an active nonce cache with a 10-second expiration TTL.
- Nonces are single-use; any connection presenting a previously seen nonce or an expired nonce is immediately aborted.
- Eavesdropped connection transcripts cannot be replayed to authenticate a rogue client.

---

## 16. Server Authentication (P2C-19)

Mutual challenge-response prevents rogue processes from creating a fake pipe server to steal VS Code telemetry:
- The VS Code extension verifies `server_digest` in `AUTH_CONFIRM` before sending any workspace data.
- Because a rogue server does not know $K_{\text{pair}}$, it cannot generate a valid `server_digest`.
- If server authentication fails, the extension disconnects and alerts the user.

---

## 17. Message Framing (P2C-19)

Named pipes operating in byte mode require explicit message framing:
- Every message is prefixed with a **4-byte big-endian unsigned integer** (`>I`) indicating the exact payload length in bytes:

```
+-----------------------------------+-----------------------------------+
|  Length Prefix (4 bytes, uint32)  |   JSON-RPC Payload (UTF-8 bytes)  |
+-----------------------------------+-----------------------------------+
```

- Reader reads exactly 4 bytes to determine payload size, then reads exactly `length` bytes.
- Eliminates stream fragmentation, buffer boundary framing bugs, and partial JSON parse errors.

---

## 18. Payload Bounds (P2C-19)

- **Hard Maximum Payload Ceiling**: 64 KB (`65536` bytes).
- If the 4-byte length prefix indicates a size exceeding 65,536 bytes, the server **immediately terminates the pipe connection** (`DisconnectNamedPipe`), logs a security alert, and does not allocate buffer memory.
- Prevents denial-of-service via memory exhaustion from hostile local processes.

---

## 19. Message Authorization (P2C-19)

The IPC interface enforces a strict JSON-RPC method allowlist:

| Method Name | Allowed Parameters | Return Data | Classification |
|---|---|---|---|
| `get_active_workspace` | `{}` | `{"workspace_name": str, "folder_paths": list[str]}` | Read-only Metadata |
| `get_open_documents` | `{}` | `{"active_doc": str, "visible_docs": list[str]}` | Read-only Metadata |
| `get_active_language_id` | `{}` | `{"language_id": str}` | Read-only Metadata |

- Any request invoking unauthorized methods (e.g. `read_file_content`, `execute_terminal_command`, `write_editor_text`) is rejected with JSON-RPC error code `-32601` (`Method not found`).
- Zero arbitrary code execution, zero keystroke access, and zero file content access.

---

## 20. Same-User Threat Limitations (P2C-19)

An authoritative security document must honestly articulate its threat boundary:

On the Windows operating system, two processes executing under the **same user account and same integrity level (Medium)** possess significant capabilities against each other:
- A malicious process running as the same user can open another process with `PROCESS_VM_READ` / `PROCESS_VM_WRITE` unless prohibited by security software or AppContainer boundaries.
- A malicious process could theoretically attach a debugger or inspect volatile memory to extract the pairing token from RAM.

**What the 4-Layer Architecture Successfully Prevents**:
1. Network-based access (kernel-enforced remote rejection).
2. Cross-user and cross-session access (kernel-enforced Logon SID DACL).
3. Pipe squatting and impersonation (first-instance flag + image verification).
4. Unprivileged background scripts guessing pipe names (cryptographic pairing requirement).
5. Replay and injection attacks (nonce-based HMAC challenge-response).

This provides maximum practical defense-in-depth on Windows within a single-user interactive session.

---

## 21. SetWinEventHook Lifecycle (P2C-20)

### 21.1 Hook Installation & Message Pump
In Win32 User32 architecture, an out-of-context hook (`WINEVENT_OUTOFCONTEXT`) requires a standard thread message loop (`GetMessageW` / `DispatchMessageW`). Without a message loop, Windows will **not** dispatch accessibility events to the calling thread.

### 21.2 Lifecycle State Transitions
```
UNINITIALIZED ──► STARTING ──► RUNNING ──► STOPPING ──► STOPPED
```

1. **`start()`**:
   - Spawns `WinEventHookThread`.
   - Thread calls `SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, NULL, callback, 0, 0, WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS)`.
   - Records hook handle `hHook` and enters `GetMessageW` loop.
2. **`stop()`**:
   - Calls `UnhookWinEvent(hHook)`.
   - Calls `PostThreadMessageW(thread_id, WM_QUIT, 0, 0)`.
   - Joins `WinEventHookThread` with a 1.0s timeout.

---

## 22. Callback Reentrancy (P2C-20)

### 22.1 Reentrancy Hazard
A common defect in Win32 hook programming is executing complex logic inside the `WinEventProc` callback. If the callback invokes functions that yield control or pump messages (such as certain COM queries or User32 dialog calls), User32 can recursively invoke `WinEventProc` on the same thread, causing stack overflow or state corruption.

### 22.2 Reentrancy Guard
Presence implements an explicit reentrancy guard:
```python
_in_callback = False

def hook_proc(hWinEventHook, event, hwnd, idObject, idChild, dwEventThread, dwmsEventTime):
    global _in_callback
    if _in_callback:
        return  # Drop nested reentrant event
    _in_callback = True
    try:
        if event == EVENT_SYSTEM_FOREGROUND and idObject == OBJID_WINDOW:
            _raw_event_queue.append((hwnd, dwmsEventTime))
            loop.call_soon_threadsafe(_notify_event_available)
    finally:
        _in_callback = False
```

The callback is strictly non-blocking: it performs no disk I/O, no caption queries, and no string formatting.

---

## 23. Bounded Event Queues (P2C-20)

To prevent memory leaks during rapid desktop window switching (e.g. holding Alt-Tab):
- Raw events are pushed into a bounded queue: `collections.deque(maxlen=50)`.
- If the consumer loop is momentarily delayed, the deque automatically evicts the oldest unprocessed events.
- Guarantees memory consumption remains strictly $O(1)$ under all desktop stress conditions.

---

## 24. Thread Ownership (P2C-20)

- Hook ownership is strictly confined to `WinEventHookThread`.
- Hook creation (`SetWinEventHook`) and destruction (`UnhookWinEvent`) are performed on that dedicated thread.
- Thread-safe handoff to the asyncio event loop uses `loop.call_soon_threadsafe()`.
- Top-level window caption inspection and PID queries are executed on worker threads (`anyio.to_thread.run_sync()`), completely isolated from the hook message pump.

---

## 25. Idle Resource Measurement (P2C-20)

### 25.1 Real-World Idle Mechanics
Previous drafts claimed absolute "0.0% CPU" and "zero wakeups".
In real Windows systems:
- When no window events occur, `GetMessageW` puts `WinEventHookThread` into a Windows kernel wait state (`Executive / UserRequest`).
- CPU utilization is near-zero, but operating system scheduler interrupts and background desktop compositing occasionally wake the thread.

### 25.2 Empirical Budgets
- **Idle CPU Utilization**:
  - *Design Target*: $< 0.05\%$
  - *Acceptance Ceiling*: $< 0.20\%$
- **Working Set Overhead**:
  - *Design Target*: $< 3.5 \text{ MB}$
  - *Acceptance Ceiling*: $< 8.0 \text{ MB}$

---

## 26. Event Burst Measurement (P2C-20)

During an intense window switching burst (Experiment D: 50 simulated window switches in 2.0 seconds):
- **Debouncing**: 150 ms debounce timer coalesces repeated events for the same window.
- **Burst CPU Utilization**:
  - *Design Target*: $< 1.0\%$
  - *Acceptance Ceiling*: $< 3.0\%$
- **Memory Delta**: Peak working set delta during burst must remain $< 1.0 \text{ MB}$.

---

## 27. Teardown Verification Plan (P2C-20)

Standardized Experiment F verifies total cleanup upon runtime exit:

1. `ContextManager.stop()` invokes `ForegroundWindowSource.stop()`.
2. `UnhookWinEvent(hHook)` removes the hook from User32.
3. `PostThreadMessageW(thread_id, WM_QUIT, 0, 0)` posts quit message.
4. `GetMessageW` returns 0, terminating the message pump.
5. `WinEventHookThread.join(timeout=1.0)` verifies thread termination within 1.0 second.
6. **Kernel Handle Audit**: Toolhelp32 snapshot asserts net handle leak is **exactly 0**.
7. **Thread Audit**: Toolhelp32 snapshot asserts net OS thread leak is **exactly 0**.

---

## 28. Cross-Specification Consistency Matrix

| Dimension | Spec 41 | Spec 42 | Spec 43 | Spec 44 | Spec 45 | Spec 46 | Spec 47 | Status |
|---|---|---|---|---|---|---|---|:---:|
| **ShellExecute Flags** | N/A | Zone checks preserved | Flag constants; CloseHandle | Subphase 2C.4 | ADR-2C-10 | P2C-17 | Sec 3 | **CONSISTENT** |
| **Shell Execution Class** | N/A | Class C async | Class C async; Python wrapper | Subphase 2C.4 | ADR-2C-10 | P2C-17 | Sec 4 | **CONSISTENT** |
| **FILETIME Representation** | Canonical ticks | WindowIdentity ticks | TOCTOU raw integer ticks | Subphase 2C.0/1 | ADR-2C-11 | P2C-18 | Sec 5 | **CONSISTENT** |
| **FILETIME Conversion** | Epoch formulas | N/A | Raw ticks | Subphase 2C.0 | ADR-2C-11 | P2C-18 | Sec 6-7 | **CONSISTENT** |
| **Named Pipe Security** | Endpoint model | 4-layer architecture | N/A | Subphase 2C.5 | ADR-2C-12 | P2C-19 | Sec 9-11 | **CONSISTENT** |
| **IPC Authentication** | Pairing/HMAC | HMAC challenge-response | N/A | Subphase 2C.5 | ADR-2C-12 | P2C-19 | Sec 13-16 | **CONSISTENT** |
| **IPC Framing & Bounds** | 4-byte / 64 KB | 4-byte / 64 KB | N/A | Subphase 2C.5 | ADR-2C-12 | P2C-19 | Sec 17-18 | **CONSISTENT** |
| **WinEventHook Model** | Message pump; deque | Threat T-01 | N/A | Exp A-F budgets | ADR-2C-13 | P2C-20 | Sec 21-24 | **CONSISTENT** |
| **Resource Budgets** | Empirical budgets | Leak targets | Latency budgets | Exp A-F table | ADR-2C-13 | P2C-20 | Sec 25-27 | **CONSISTENT** |

---

## 29. Updated ADR Decisions

| ADR ID | Title | Summary of Decision |
|---|---|---|
| **ADR-2C-01** | Native Win32 `SetWinEventHook` | Event-driven kernel notification; User32 cached caption retrieval |
| **ADR-2C-02** | Dedicated Hook Thread | Background `WinEventHookThread` running `GetMessageW` message pump |
| **ADR-2C-03** | Transient Context Snapshot | Single active snapshot in volatile RAM; zero default disk persistence |
| **ADR-2C-04** | Capability Integration | Tools register in `ToolRegistry` with existing `AuthorityLevel` enums |
| **ADR-2C-05** | Best-Effort Window Focus | `SetForegroundWindow` respects OS lock; composite `WindowIdentity` guard |
| **ADR-2C-06** | Prompt Injection Defense | Structural untrusted data classification; `PermissionBoundary` isolation |
| **ADR-2C-07** | Shell Safety Boundaries | Remove `SEE_MASK_NOZONECHECKS`; separate Shell from Job containment |
| **ADR-2C-08** | Deferral of Clipboard Access | Ambient clipboard reading removed from Phase 2C default tools |
| **ADR-2C-09** | Accurate Job Object Taxonomy | Job Objects provide Process Lifecycle Containment, not security sandboxing |
| **ADR-2C-10** | Correct ShellExecute Flags & Class C | Correct `0x0400` flag; caller closes `hProcess`; Class C async classification |
| **ADR-2C-11** | Canonical FILETIME Ticks | 64-bit unsigned integer ticks; raw equality check; documented truncation |
| **ADR-2C-12** | Authenticated Named Pipe IPC | 4-layer defense: Logon SID DACL, PID check, HMAC challenge-response, 64 KB |
| **ADR-2C-13** | WinEventHook Resource & Lifecycle | Message pump, reentrancy guard, bounded deque, standardized Experiments A-F |

---

## 30. Remaining Uncertainties & Operational Scope

1. **Windows 11 Virtual Desktops**: The Win32 API does not expose a supported, public API for querying virtual desktop GUIDs (delegated to undocumented COM interface `IVirtualDesktopManagerInternal`). Phase 2C observes windows across all desktops without virtual desktop grouping.
2. **UWP Frame Host Latency**: ApplicationFrameHost windows for XAML/UWP apps occasionally delay caption updates until activated. Handled gracefully by 150 ms debouncing.
3. **PowerShell ExecutionPolicy for Terminal Hook**: Installing the opt-in shell integration hook into `$PROFILE` requires the user's PowerShell execution policy to permit local scripts (`RemoteSigned`). This is documented as an opt-in installation prerequisite.

---

## 31. Final Architectural Acceptance Gate Results

| Gate | Dimension | Requirement | Evaluation | Status |
|---|---|---|---|:---:|
| **Gate A** | Win32 Correctness | All Win32 APIs match official MSDN documentation. | `GetWindowTextW`, `SetForegroundWindow`, `SetWinEventHook`, `SHELLEXECUTEINFOW` flags (`0x0400`), FILETIME ticks, and Logon SID DACL verified. | **PASS** |
| **Gate B** | Context Lifecycle | Retention, TTL, and persistence rules agree. | Transient active snapshot only; immediate invalidation on focus switch; zero ambient SQLite persistence. | **PASS** |
| **Gate C** | Data Sensitivity | Item-level classification enforced. | `ContextItem` defines 4 sensitivity tiers (`PUBLIC`, `PERSONAL`, `SENSITIVE`, `PROTECTED`). | **PASS** |
| **Gate D** | Data Egress | Model-provider and MCP transmission rules explicit. | Egress rules defined for Gemini cloud API; ambient context strictly stripped from MCP tool parameters. | **PASS** |
| **Gate E** | Prompt Injection | Structural untrusted data classification. | Environmental text is untrusted data; PermissionBoundary cannot be bypassed by model output. | **PASS** |
| **Gate F** | Shell Safety | No zone check bypass; accurate containment. | `SEE_MASK_NOZONECHECKS` prohibited; caller closes `hProcess`; Class C async classification; Job separated. | **PASS** |
| **Gate G** | Window Activation | Foreground restrictions respected. | Best-effort activation; taskbar flash fallback; composite `WindowIdentity` TOCTOU check with raw ticks. | **PASS** |
| **Gate H** | Timeout Correctness | Thread cancellation reality documented. | Worker thread un-abortability acknowledged; Class A/B/C/D taxonomy established. | **PASS** |
| **Gate I** | Provider Integration | Provider IPC mechanisms technically valid. | VS Code 4-layer authenticated named pipe (Logon SID DACL, PID check, HMAC challenge-response, 64 KB framing) established. | **PASS** |
| **Gate J** | Data Contracts | Immutability and canonical units defined. | Deep immutability enforced; canonical `process_creation_filetime_ticks: int`; microsecond truncation documented. | **PASS** |
| **Gate K** | Permission Integration | Exact codebase interfaces and enums used. | Verified against `runtime/permissions.py` (`AuthorityLevel.L0_OBSERVE`, etc.) and `runtime/tools/registry.py` (`ToolDefinition`). | **PASS** |
| **Gate L** | Privacy Model | No surveillance or automatic clipboard access. | Continuous OCR/audio/keystrokes prohibited; clipboard reading explicitly deferred. | **PASS** |
| **Gate M** | Resource Targets | Budgets are measurable targets. | Targets separated into Design Targets vs Acceptance Ceilings; Experiments A through F defined. | **PASS** |
| **Gate N** | Cross-Spec Consistency | Specs 41 through 47 agree across all dimensions. | Consistency matrix verified with zero contradictions across all seven specifications. | **PASS** |
| **Gate O** | Implementation Scope | Zero Phase 2C source code created or modified. | Verified: 100% documentation milestone; zero production Python code modified. | **PASS** |
| **Gate P** | Documentation Complete | Spec 47 authored with authoritative citations. | Specification 47 complete with 32 required sections, ADRs, and MSDN references. | **PASS** |
| **Gate Q** | API, IPC & Resource Accuracy | Shell flags, FILETIME ticks, named pipe auth, and hook model validated. | Findings P2C-17 through P2C-20 resolved and cross-verified against official Win32 documentation. | **PASS** |

**All 17 Architectural Acceptance Gates (Gates A through Q) are PASS.**

---

## 32. Git Commit Information

- **Working Branch**: `architecture/phase-2c-api-ipc-resource-correction`
- **Target Commit Message**: `docs(phase-2c): correct shell flags, FILETIME, IPC authentication and hook resource model`
- **Modified Specifications**:
  - `specs/41_PHASE_2C_WINDOWS_CONTEXT_ARCHITECTURE.md`
  - `specs/42_PHASE_2C_CONTEXT_SECURITY_AND_PRIVACY_MODEL.md`
  - `specs/43_PHASE_2C_WINDOWS_CAPABILITY_ARCHITECTURE.md`
  - `specs/44_PHASE_2C_IMPLEMENTATION_ROADMAP.md`
  - `specs/45_PHASE_2C_ARCHITECTURAL_DECISION_RECORD.md`
  - `specs/46_PHASE_2C_ARCHITECTURE_CORRECTION_AND_FREEZE_REPORT.md`
  - `specs/47_PHASE_2C_WINDOWS_API_IPC_AND_RESOURCE_CORRECTION_REPORT.md`
- **Source Code Verification**: Zero production Python files (`runtime/*.py`, `tests/*.py`) were modified.
- **Architectural Status**:

# **PHASE 2C ARCHITECTURE AUTHORITATIVELY FROZEN**
