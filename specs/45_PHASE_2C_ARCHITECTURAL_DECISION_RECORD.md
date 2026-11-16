# SPECIFICATION 45 — PHASE 2C: ARCHITECTURAL DECISION RECORD (ADR) (CORRECTED)
# Persistent Intelligent Presence — Architectural Decisions, Technology Evaluations & Trade-offs

## 1. Executive Summary & Context

Phase 2C defines the integration of ambient Windows context observation and native desktop capability execution into the Presence Runtime.

This document records the formal Architectural Decision Records (ADRs) governing Phase 2C. Every architectural decision is justified by technical constraints, security invariants, performance benchmarks, and a thorough evaluation of existing open-source and native solutions.

---

## 2. Reuse-First Technology Evaluation Matrix

In accordance with Presence engineering principles, mature open-source projects, native APIs, and Python libraries were investigated before making architectural decisions:

| Candidate Solution | Domain | Maintenance & Maturity | Windows Compatibility | Footprint & Dependencies | Security & Permissions | Evaluation & Decision |
|---|---|---|---|---|---|---|
| **Native Win32 APIs via `ctypes`** | Context & Capabilities | Core Windows OS (Universal) | 100% Windows 10/11 | Zero external dependencies (Python stdlib) | Exact Win32 access mask control | **REUSE / ADAPT (PRIMARY)**: Zero bloat, total handle lifetime control, no binary wheels. |
| **`pywin32` (win32api/win32gui)** | Win32 Python Wrapper | Highly mature, well-maintained | Broad Windows support | Large binary wheel (~15 MB C-extensions) | Wraps Win32, but adds heavy DLL loading | **REJECTED**: `ctypes` provides identical native capability without 15 MB binary baggage. |
| **`pywinauto`** | Desktop GUI Automation | Active open-source | Windows 7-11 | Heavy dependencies (comtypes, six) | Full UI automation tree walking is high-risk | **REJECTED**: Too heavy for persistent idle presence; excessive thread overhead. |
| **`uiautomation` (yinkaisheng)** | Windows UI Automation | Maintained | Windows 10/11 (COM UIA) | Light COM wrapper | Tree traversal can freeze target applications | **ADAPT (TARGETED)**: Useful only for on-demand text inspection; rejected for default context. |
| **Screenpipe** | Ambient Screen/Audio Capture | Active Rust codebase | Windows/macOS | Very heavy (Rust daemon, FFmpeg, SQLite, OCR) | Continuous recording violates Data Minimization | **REJECTED**: Incompatible with Presence zero-surveillance and lightweight invariants. |
| **`psutil`** | Process & Resource Telemetry | Highly mature, standard | Windows 10/11 | Already verified in Presence runtime | Query limited process information | **REUSE**: Standardized process resource queries and handle telemetry. |
| **Windows Notification Hook (`SetWinEventHook`)** | Ambient Window Changes | Core Windows OS | Windows 2000 to Windows 11 Build 26100 | Pure native Win32 | Out-of-context hook requires zero DLL injection | **REUSE / BUILD**: Provides 100% reactive, zero-polling desktop awareness. |

---

## 3. Formal Architectural Decision Records

### ADR-2C-01: Windows Context Acquisition via Native `SetWinEventHook`
- **Status**: **APPROVED**
- **Context**: Presence requires awareness of when the user switches applications or active windows. Continuous polling (e.g. calling `GetForegroundWindow` in a sleep loop every 500 ms) consumes continuous CPU cycles, wastes battery on laptops, and adds unnecessary wakeups.
- **Decision**: Use `SetWinEventHook` with `EVENT_SYSTEM_FOREGROUND` (`0x0003`) and `WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS`.
- **Top-Level Caption Semantics (P0-01)**: For top-level windows in other processes, `GetWindowTextW` retrieves the caption directly from the internal window structure in the desktop heap without sending cross-process `WM_GETTEXT` messages. No synthetic timeout wrappers are necessary for top-level captions.
- **Consequences**:
  - *Positive*: Pure event-driven notification from the Windows kernel. Zero CPU utilization when the desktop is static.
  - *Positive*: Out-of-context hook avoids injecting DLLs into target processes, preventing antivirus heuristic alerts.
  - *Requirement*: Out-of-context hooks require a standard Win32 message pump on the installing thread. Handled via dedicated `WinEventHookThread`.

---

### ADR-2C-02: Dedicated Background Message-Pump Thread for Event Hooks
- **Status**: **APPROVED**
- **Context**: Win32 event hooks (`SetWinEventHook`) delivered with `WINEVENT_OUTOFCONTEXT` require a thread message queue processed by `GetMessageW` / `DispatchMessageW`. The main Presence runtime thread runs an asynchronous `asyncio` event loop and cannot host a blocking Win32 message loop without severe degradation.
- **Decision**: Spawn a dedicated background OS thread (`WinEventHookThread`) owning the hook and message pump.
- **Consequences**:
  - *Positive*: Isolates OS message processing from asyncio scheduling.
  - *Positive*: Event dispatch to asyncio loop uses thread-safe `call_soon_threadsafe()`.
  - *Resource Impact*: Adds exactly 1 native OS thread, well within the 8-thread ceiling budget.

---

### ADR-2C-03: Transient In-Memory Context Snapshot vs Persistent History (P0-02)
- **Status**: **APPROVED**
- **Context**: Should ambient context maintain a historical ring buffer of applications or write every window switch to SQLite?
- **Decision**: **No.** Environmental context is transient and held strictly as the **single active `ContextSnapshot` in volatile RAM**. No history ring buffer of previously focused windows is maintained. Context is written to SQLite **only** when an active `Step` in an executed agent turn explicitly references or acts upon that context.
- **Consequences**:
  - *Positive*: Prevents surveillance tracking and unbounded SQLite database growth from normal user window switching.
  - *Positive*: Protects user privacy: ambient window history is wiped completely when Presence exits.
  - *Positive*: SQLite trajectory schema (`runtime/trajectory.py`) remains 100% frozen with zero migration needed.

---

### ADR-2C-04: Capability Integration through Existing `ToolRegistry` and `PermissionBoundary` (P1-07)
- **Status**: **APPROVED**
- **Context**: When introducing native Windows capabilities (e.g. `desktop.activate_window`, `shell.open_path`), should a new "capability execution engine" be created?
- **Decision**: **No.** Native capabilities must register as standard tools in `ToolRegistry` and enforce authorization through `PermissionBoundary` using the verified existing `AuthorityLevel` enums (`L0_OBSERVE`, `L1_RECOMMEND`, `L2_EXECUTE_REVERSIBLE`, `L3_PROTECTED`).
- **Consequences**:
  - *Positive*: Preserves single unified execution path: `AgentExecutor -> ToolRegistry -> PermissionBoundary`.
  - *Positive*: Automatically gains existing multi-tier security: `L0_OBSERVE` for read-only metadata, `L2_EXECUTE_REVERSIBLE` for desktop actions, and `L3_PROTECTED` with HMAC `ConsentToken` for sensitive data.
  - *Positive*: Full audit logging in trajectory steps is inherited automatically.

---

### ADR-2C-05: Best-Effort Reversible Window Activation over Synthetic Input Injection (P0-06)
- **Status**: **APPROVED**
- **Context**: Should Presence support synthetic mouse clicks and keyboard typing (`SendInput`) or force foreground focus using `AttachThreadInput`?
- **Decision**: **Reject synthetic input injection and `AttachThreadInput`.** Phase 2C capabilities are restricted to clean, reversible desktop actions. Window activation via `desktop.activate_window` is implemented as a **best-effort operation** adhering strictly to Windows foreground lock policies. If Windows denies foreground activation, Presence calls `FlashWindowEx()` to alert the user on the taskbar.
- **Window Identity TOCTOU Guard (P1-05)**: HWNDs can be recycled. Activation requires composite verification of `HWND`, `PID`, and process creation time via `GetProcessTimes` before calling `SetForegroundWindow`.
- **Consequences**:
  - *Positive*: Eliminates UI lockups, thread message corruption, and input deadlocks caused by `AttachThreadInput`.
  - *Positive*: Reversible window activation allows Presence to bring relevant windows to the user's attention safely without usurping user mouse control.

---

### ADR-2C-06: Defense-in-Depth for Untrusted Environmental Text (P0-04)
- **Status**: **APPROVED**
- **Context**: Can XML delimiters and angle-bracket sanitization alone prevent prompt injection?
- **Decision**: **No.** Natural language instructions ("Ignore previous instructions...") do not need XML tags. Delimiters are only defense-in-depth formatting.
- **Architectural Defense**:
  1. Environmental context is structured as untrusted data fields.
  2. Model is constitutionally instructed that environmental text represents unverified user-space data, never instructions.
  3. Model cannot grant its own permissions or create consent tokens.
  4. Destructive tools require out-of-band HMAC-bound `ConsentToken`.
- **Consequences**:
  - *Positive*: Establishes true structural defense-in-depth against prompt injection.
  - *Positive*: Even if an LLM is deceived by a caption, the `PermissionBoundary` blocks unauthorized tool execution.

---

### ADR-2C-07: Respect for Windows Security Zones & Disambiguation of Shell vs Process Containment (P0-05, P1-08)
- **Status**: **APPROVED**
- **Context**: Should `ShellExecuteEx` use `SEE_MASK_NOZONECHECKS`, and is `ShellExecuteEx` Job Object contained?
- **Decision**:
  1. **Remove `SEE_MASK_NOZONECHECKS`**: Presence strictly respects Windows attachment security zones (`IAttachmentExecute`) and Mark-of-the-Web safety warnings.
  2. **Disambiguate Shell from Process Containment**: `ShellExecuteExW` delegates to the Windows shell and may invoke existing processes, DDE, or COM servers. It does **not** guarantee a new process handle or Job Object containment.
  3. Contained process execution must use `Win32SuspendedLauncher` (direct `CreateProcessW` with `CREATE_SUSPENDED` + Job Object assignment).
- **Consequences**:
  - *Positive*: Preserves Windows OS security zone protections.
  - *Positive*: Honest, technically accurate distinction between shell document opening and contained process execution.

---

### ADR-2C-08: Deferral of Clipboard Access from Default Capabilities (P1-06)
- **Status**: **APPROVED**
- **Context**: Should `presence.read_clipboard` be included in Phase 2C, and can Python guarantee memory zeroization?
- **Decision**: **Defer clipboard reading from Phase 2C.** Ambient clipboard reading violates data minimization. If implemented in a future milestone, it must be strictly user-initiated, require explicit single-use L3 consent, and clearly acknowledge Python GC / string interning limitations (no false promises of guaranteed memory zeroization).
- **Consequences**:
  - *Positive*: Eliminates critical accidental credential harvesting vector.
  - *Positive*: Prevents clipboard content from leaking into external model prompts.

---

### ADR-2C-09: Accurate Job Object Terminology (P1-08)
- **Status**: **APPROVED**
- **Context**: Is a Win32 Job Object a complete security sandbox?
- **Decision**: **No.** A Win32 Job Object provides **process lifecycle containment** (kill-on-close, child termination) and **resource management** (CPU/RAM limits). It does **not** provide filesystem isolation, network isolation, or AppContainer token restriction.
- **Consequences**:
  - *Positive*: Eliminates inaccurate security claims in technical documentation.

---

### ADR-2C-10: Correct ShellExecute Flag Usage and Asynchronous Execution Policy (P2C-17)
- **Status**: **APPROVED**
- **Context**: Previous specification drafts confused `SEE_MASK_FLAG_NO_UI` (0x00000400) with `SEE_MASK_NOCLOSEPROCESS` (0x00000040), assumed `ShellExecuteExW` provided a native timeout or Job Object containment, and classified `shell.open_path` as Class B (hard timeout).
- **Decision**:
  1. Define exact flag constants:
     - `SEE_MASK_NOCLOSEPROCESS = 0x00000040`: Requests `hProcess` if a new process instance is created.
     - `SEE_MASK_FLAG_NO_UI = 0x00000400`: Suppresses non-critical shell error message dialogs. Crucially, Windows security dialogs (Mark-of-the-Web, Attachment Execution Service) remain exempt by OS design and are still presented when required.
     - `SEE_MASK_NOZONECHECKS = 0x00000008`: Strictly prohibited. Must never be set.
     - `SEE_MASK_NOASYNC = 0x00000100`: Forces synchronous execution on caller thread. Does NOT provide a timeout; it makes the call block until shell completes initialization.
     - `SEE_MASK_ASYNCOK = 0x00100000`: Allows asynchronous execution on a background thread created by the Windows shell.
  2. Implement caller handle ownership: if `sei.hProcess` is returned, Presence owns the handle and immediately closes it via `CloseHandle()`.
  3. Reclassify `shell.open_path` from Class B to **Class C (Best-Effort Asynchronous Shell Request)**. Python wraps the call with `anyio.fail_after(5.0)`, with explicit architectural acknowledgement that if the underlying thread blocks in DDE or an out-of-process COM server, the thread cannot be forcibly aborted without terminating the Python host process.
- **Alternatives Considered**:
  - *CreateProcessW with manual registry association parsing*: Requires manual lookup in `HKEY_CLASSES_ROOT`, verb resolution, DDE handling, and UAC elevation handling. Highly fragile, maintenance-heavy, and prone to compatibility breakage. Rejected.
  - *Python `os.startfile`*: Standard library wrapper around `ShellExecuteW`. Rejects flags, does not expose `fMask` or `hProcess`, cannot suppress non-security error dialogs, and cannot permit asynchronous dispatch via `SEE_MASK_ASYNCOK`. Rejected.
  - *Hard-killing worker threads on timeout (`TerminateThread`)*: Corrupts Python runtime internals, deadlocks the GIL, leaves critical sections locked, and leaks OS handles. Strictly prohibited by Presence engineering standards.
- **Technical Justification**: Calling `ShellExecuteExW` with `SEE_MASK_FLAG_NO_UI | SEE_MASK_ASYNCOK | SEE_MASK_NOCLOSEPROCESS` delegates association resolution to the registered shell handler while permitting asynchronous dispatch and giving Presence immediate control to close any spawned process handle.
- **Security Consequences**: Respecting Windows security zones preserves Mark-of-the-Web and Attachment Execution Service protections. Closing `hProcess` prevents handle exhaustion attacks. Executable blacklist prevents direct execution of scripts or binaries through `open_path`.
- **Resource Consequences**: Handing off to the shell's background thread pool minimizes calling thread blocking. Closing `hProcess` prevents kernel handle leaks.
- **Limitations**: If a shell association delegates to DDE (e.g. legacy Office) or an unresponsive COM server, the calling worker thread may block until the OS COM timeout expires. The Python coroutine cancels at 5.0s, but the thread remains until unblocked.
- **Microsoft References**:
  - [SHELLEXECUTEINFOW structure (shellapi.h)](https://learn.microsoft.com/en-us/windows/win32/api/shellapi/ns-shellapi-shellexecuteinfow)
  - [ShellExecuteExW function (shellapi.h)](https://learn.microsoft.com/en-us/windows/win32/api/shellapi/nf-shellapi-shellexecuteexw)

---

### ADR-2C-11: Canonical FILETIME Process Identity Representation & Exact Conversion (P2C-18, P2C-23)
- **Status**: **APPROVED**
- **Context**: Previous specifications stored process creation time as `process_creation_time_ns` and converted to Python `datetime.datetime` using floating-point division (`unix_ns / 1_000_000_000.0`), reintroducing float rounding error and sub-microsecond ambiguity.
- **Decision**:
  1. Store process creation time canonically as `process_creation_filetime_ticks: int` (raw 64-bit unsigned integer count of 100-nanosecond intervals since January 1, 1601 UTC).
  2. Implement canonical integer conversion formulas:
     - `FILETIME_UNIX_EPOCH_OFFSET_TICKS = 116444736000000000`
     - `unix_100ns = ticks - FILETIME_UNIX_EPOCH_OFFSET_TICKS`
     - `unix_ns = unix_100ns * 100`
     - `unix_seconds_numerator = unix_100ns`, `unix_seconds_denominator = 10_000_000`
  3. Window identity and TOCTOU validation compare raw 64-bit integer ticks directly (`current_ticks == expected_ticks`), eliminating all float and datetime rounding artifacts. Zero float conversion occurs in the canonical identity path.
  4. Human-readable `datetime.datetime` conversion uses pure integer arithmetic:
     ```python
     def filetime_ticks_to_utc_datetime(ticks: int) -> datetime:
         unix_100ns = ticks - FILETIME_UNIX_EPOCH_OFFSET_TICKS
         unix_microseconds = unix_100ns // 10
         epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
         return epoch + timedelta(microseconds=unix_microseconds)
     ```
  5. Document that integer floor division `// 10` drops sub-microsecond precision (`unix_100ns % 10`, 0-900 ns) because Python `datetime` has microsecond resolution. The raw tick value is never altered.
- **Alternatives Considered**:
  - *Floating-point Unix timestamps*: 64-bit IEEE 754 floats have 53 bits of mantissa, losing precision for nanosecond timestamps. Rejected.
  - *Python `datetime.datetime` for TOCTOU validation*: Microsecond precision discards the lowest digit of 100ns FILETIME, causing false TOCTOU mismatch on rapid process reuse. Rejected.
  - *Float-based datetime constructor (`datetime.fromtimestamp(unix_seconds)`)*: Reintroduces IEEE 754 floating-point rounding errors. Rejected.
- **Technical Justification**: Combining low and high DWORDs into a Python integer preserves the exact 64-bit kernel value. Using integer arithmetic for timedelta avoids float precision loss.
- **Security Consequences**: Prevents PID-recycling TOCTOU vulnerabilities by ensuring byte-for-byte exact equality checks between the observed window and the activated window.
- **Resource Consequences**: Zero memory or computational overhead; integer comparison in Python is a single-cycle operation.
- **Limitations**: Human-readable display truncates sub-microsecond precision (0-900 ns).
- **Microsoft References**:
  - [FILETIME structure (minwinbase.h)](https://learn.microsoft.com/en-us/windows/win32/api/minwinbase/ns-minwinbase-filetime)
  - [GetProcessTimes function (processthreadsapi.h)](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getprocesstimes)

---

### ADR-2C-12: Authenticated Local Named Pipe IPC for Developer Context (P2C-19, P2C-21, P2C-22, P2C-26, P2C-27)
- **Status**: **APPROVED**
- **Context**: Developer tools like VS Code hold valuable context (active workspace, open files, language). Unauthenticated or ambient IPC introduces severe security risks: pipe squatting, cross-session access expansion, unauthorized model steering, and data harvesting.
- **Decision**: Implement a 4-layer authenticated named pipe IPC architecture for VS Code context extraction:
  1. **Transport Access Control & Parameter Separation (P2C-21, P2C-22)**:
     - Pipe name `\\.\pipe\presence-vscode-<username>-<logon_session_id>`.
     - `dwOpenMode`: `PIPE_ACCESS_DUPLEX (0x3) | FILE_FLAG_FIRST_PIPE_INSTANCE (0x80000) | FILE_FLAG_OVERLAPPED (0x40000000)`. Fails closed if squatted.
     - `dwPipeMode`: `PIPE_TYPE_BYTE (0x0) | PIPE_READMODE_BYTE (0x0) | PIPE_REJECT_REMOTE_CLIENTS (0x8) | PIPE_WAIT (0x0)`. Rejects remote clients at NPFS kernel level.
     - Hardened DACL: `D:P(A;;GA;;;OW)(A;;0x12019b;;;<LogonSID>)`. Broad User SID grant is completely removed, eliminating cross-session access expansion.
     - Client rights: `0x0012019B` strictly withholds `FILE_CREATE_PIPE_INSTANCE` (`0x0004`).
     - `bInheritHandle = FALSE` prevents child process inheritance.
  2. **Peer Process Identity Verification**: Server obtains client PID via `GetNamedPipeClientProcessId()`, validates executable path via `QueryFullProcessImageNameW()` against `Code.exe`, captures process creation ticks via `GetProcessTimes()`, and asserts client token Logon SID matches server Logon SID.
  3. **Application Pairing & Bound Transcript Mutual Authentication (P2C-26)**:
     - User-mediated pairing command: `presence vscode-pair` (256-bit secret, 5-minute TTL, single-use, DPAPI-protected, no secrets in CLI flags/env vars/logs).
     - Full transcript binding: `T = f"2.0|{pipe_endpoint}|{server_pid}|{client_pid}|{client_creation_filetime_ticks}|{client_logon_sid}|{server_nonce}|{client_nonce}"`.
     - Domain separation labels: `PRESENCE_VSCODE_CLIENT_AUTH_V1` and `PRESENCE_VSCODE_SERVER_AUTH_V1`. Constant-time digest verification (`secrets.compare_digest`). Ephemeral session key derived.
  4. **Framing, Payload Bounds & Canonical Operation Allowlist (P2C-27)**:
     - 4-byte big-endian length prefix; 64 KB hard max payload ceiling.
     - Canonical operation allowlist: `vscode.ping`, `vscode.get_active_workspace`, `vscode.get_active_editor_metadata`, `vscode.get_open_documents`.
     - `provider.shutdown` is strictly prohibited. Read-only metadata only (zero file contents, zero code execution).
- **Alternatives Considered**:
  - *Local HTTP Server (localhost)*: Vulnerable to DNS rebinding and cross-site scripting port scanning. Rejected.
  - *Broad User SID DACL*: Permits cross-session access to other logon sessions of the same account. Rejected.
  - *Granting GENERIC_WRITE to client*: Grants `FILE_CREATE_PIPE_INSTANCE` (0x0004). Rejected.
  - *Unbound Nonce Challenge*: Fails to bind process identities and endpoint to authentication proof. Rejected.
- **Technical Justification**: Combining Logon SID DACL with specific rights withholding 0x0004, first-instance flag, remote rejection, process identity verification, and transcript-bound HMAC provides maximum practical defense-in-depth on Windows.
- **Security Consequences**: Prevents cross-session eavesdropping, pipe squatting, replay attacks, and instance creation attacks.
- **Resource Consequences**: Ephemeral IPC connection queried only on window focus change. Timeout capped at 500 ms. Minimal RAM footprint (< 50 KB).
- **Limitations**: Medium-integrity processes executing within the same interactive user session could theoretically inspect process memory. This boundary is explicitly documented.
- **Microsoft References**:
  - [Named Pipe Security and Access Rights](https://learn.microsoft.com/en-us/windows/win32/ipc/named-pipe-security-and-access-rights)
  - [CreateNamedPipeW function (namedpipeapi.h)](https://learn.microsoft.com/en-us/windows/win32/api/namedpipeapi/nf-namedpipeapi-createnamedpipew)
  - [GetNamedPipeClientProcessId function (winbase.h)](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-getnamedpipeclientprocessid)

---

### ADR-2C-13: SetWinEventHook Resource, Lifecycle & Bounded Dispatch Model (P2C-20, P2C-24, P2C-25)
- **Status**: **APPROVED**
- **Context**: WinEvent hooks require a message loop, callbacks can be reentered, and notifying the asyncio event loop on every raw event creates an unbounded notification queue under burst conditions. Furthermore, hook destruction ownership must belong strictly to the installing thread.
- **Decision**:
  1. Implement dedicated `WinEventHookThread` hosting `GetMessageW` message pump. Idle CPU achieves kernel wait (< 0.05% design target, < 0.20% ceiling).
  2. Implement reentrancy guard (`_in_callback` flag) and bounded raw queue (`collections.deque(maxlen=50)`).
  3. Implement **Single-Pending-Notification / Coalescing Mechanism (P2C-24)**:
     - Atomic/synchronized flag `_notification_pending: bool`.
     - Callback appends event to raw deque (overflow drops oldest event; increments `events_dropped`).
     - If `_notification_pending` is True, increments `events_coalesced` and suppresses additional scheduling.
     - If `_notification_pending` is False, sets flag to True and schedules `loop.call_soon_threadsafe(state.on_event)`.
     - Consumer drains raw deque, processes latest event, and resets flag to False.
     - Invariant: `pending_notification_count <= 1` at all times.
  4. Implement **Owner-Controlled Teardown Lifecycle (P2C-25)**:
     - `UnhookWinEvent(self._hHook)` is executed strictly by `WinEventHookThread` upon receiving `WM_QUIT`.
     - Runtime thread joins hook thread with 1.0s timeout. If alive after timeout, reports `HookShutdownTimeoutError` (do not claim clean termination).
  5. Establish seven standardized verification experiments (A through G, including Experiment G Stalled Consumer Backpressure Stress Test).
- **Alternatives Considered**:
  - *Scheduling notification on every event*: Floods asyncio event loop with unbounded callbacks during Alt-Tab storms. Rejected.
  - *Unhooking from runtime thread*: Violates Win32 thread affinity for hooks, introducing race conditions. Rejected.
  - *Assuming thread termination on timeout*: Masked hung thread bugs. Rejected.
- **Technical Justification**: Separating minimal event queuing from single-pending-notification asyncio handoff guarantees an end-to-end bounded dispatch pipeline ($O(1)$ memory).
- **Security Consequences**: Prevents UI stuttering and eliminates denial-of-service vectors against the desktop message loop.
- **Resource Consequences**: Idle CPU drops to kernel wait. Burst events capped at 50 in deque and exactly 1 pending notification callback.
- **Limitations**: Dropping oldest events during Alt-Tab bursts prioritizes the latest foreground window state.
- **Microsoft References**:
  - [SetWinEventHook function (winuser.h)](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwineventhook)
  - [Guarding Against Reentrancy in Hook Functions](https://learn.microsoft.com/en-us/windows/win32/winauto/guarding-against-reentrancy-in-hook-functions)
  - [UnhookWinEvent function (winuser.h)](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-unhookwinevent)

---

## 4. Summary of Architectural Decisions Table

| Decision ID | Area | Decision | Primary Technical Justification |
|---|---|---|---|
| **ADR-2C-01** | Context Acquisition | Native Win32 `SetWinEventHook` + `GetWindowTextW` | Event-driven kernel notification; near-zero idle CPU; cached caption |
| **ADR-2C-02** | Threading | Dedicated `WinEventHookThread` | Isolates blocking Win32 message pump from asyncio loop |
| **ADR-2C-03** | Storage | Transient single active snapshot (RAM only) | Zero database bloat; privacy preservation; no ambient history |
| **ADR-2C-04** | Capabilities | Native tools in `ToolRegistry` (`AuthorityLevel`) | Single execution path; unified PermissionBoundary; exact enums |
| **ADR-2C-05** | Window Focus | Best-effort activation + `WindowIdentity` guard | Eliminates `AttachThreadInput` deadlocks; prevents HWND recycling TOCTOU |
| **ADR-2C-06** | Prompt Injection | Structural untrusted data + PermissionBoundary | Natural language injection immunity; delimiters are only formatting |
| **ADR-2C-07** | Shell Safety | Remove `SEE_MASK_NOZONECHECKS`; separate Shell/Job | Preserves security zones; accurate containment boundaries |
| **ADR-2C-08** | Clipboard | Defer clipboard access from default Phase 2C | Eliminates credential harvesting; realistic memory zeroization |
| **ADR-2C-09** | Terminology | Job Objects = Process Lifecycle Containment | Accurate Windows OS security taxonomy (not a security sandbox) |
| **ADR-2C-10** | Shell Flags | Correct `SEE_MASK_FLAG_NO_UI`, handle ownership & Class C | Fixed constant confusion; caller closes `hProcess`; async shell classification |
| **ADR-2C-11** | Process Identity | Canonical FILETIME ticks & integer arithmetic | 64-bit unsigned ticks; integer timedelta conversion; zero float in identity path |
| **ADR-2C-12** | Local IPC | Hardened named pipe DACL & bound transcript HMAC | Logon SID only; withhold 0x0004; parameter separation; transcript HMAC; canonical allowlist |
| **ADR-2C-13** | Hook Model | Coalesced WinEvent dispatch & owner teardown | Single-pending notification (`<= 1`); owner unhooks; timeout failure reporting |

---

## 5. Architectural Consistency & Gate Readiness

Specifications 41 through 45 are now completely reconciled, internally consistent, and fully verified against Microsoft Learn documentation and actual Presence source code. The architecture is ready for final Architectural Acceptance Gate evaluation in Specification 46, Specification 47, and Specification 48.
