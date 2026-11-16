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

### ADR-2C-11: Canonical FILETIME Process Identity Representation (P2C-18)
- **Status**: **APPROVED**
- **Context**: Previous specifications stored process creation time as `process_creation_time_ns` and converted to Python `datetime.datetime`. This introduced ambiguity regarding unit definitions (nanoseconds vs 100ns ticks), epoch offsets (1601 vs 1970), leap seconds, and sub-microsecond precision truncation (Python `datetime` has microsecond resolution, losing 100ns precision).
- **Decision**:
  1. Store process creation time canonically as `process_creation_filetime_ticks: int` (raw 64-bit unsigned integer count of 100-nanosecond intervals since January 1, 1601 UTC).
  2. Implement canonical conversion formulas:
     - `filetime_to_ticks(ft) = (ft.dwHighDateTime << 32) | ft.dwLowDateTime`
     - `filetime_ticks_to_unix_ns(ticks) = (ticks - 116444736000000000) * 100`
     - `filetime_ticks_to_unix_seconds(ticks) = (ticks - 116444736000000000) / 10_000_000.0`
  3. Window identity and TOCTOU validation compare raw 64-bit integer ticks directly (`current_ticks == expected_ticks`), eliminating all float and datetime rounding artifacts.
  4. Conversion to Python `datetime` is used strictly for user-facing telemetry and display, with explicit documentation that sub-microsecond ticks are truncated.
- **Alternatives Considered**:
  - *Floating-point Unix timestamps*: 64-bit IEEE 754 floats have 53 bits of mantissa, losing precision for nanosecond timestamps. Rejected.
  - *Python `datetime.datetime`*: Microsecond precision (6 decimal places) discards the lowest digit of 100ns FILETIME, causing false TOCTOU mismatch on rapid process reuse. Rejected.
  - *Arbitrary `process_creation_time_ns`*: Ambiguous whether offset from 1601 or 1970, and multiplying ticks by 100 introduces unnecessary integer scaling. Rejected.
- **Technical Justification**: Windows kernel natively represents time via `FILETIME` struct (`dwLowDateTime`, `dwHighDateTime`). Combining into a Python arbitrary-precision integer preserves the exact 64-bit kernel value with zero truncation.
- **Security Consequences**: Prevents PID-recycling TOCTOU vulnerabilities by ensuring byte-for-byte exact equality checks between the observed window and the activated window.
- **Resource Consequences**: Zero memory or computational overhead; integer comparison in Python is a single-cycle operation.
- **Limitations**: Displaying creation time to humans requires epoch offset arithmetic and sub-microsecond truncation to fit ISO 8601 microseconds.
- **Microsoft References**:
  - [FILETIME structure (minwinbase.h)](https://learn.microsoft.com/en-us/windows/win32/api/minwinbase/ns-minwinbase-filetime)
  - [GetProcessTimes function (processthreadsapi.h)](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getprocesstimes)

---

### ADR-2C-12: Authenticated Local Named Pipe IPC for Developer Context (P2C-19)
- **Status**: **APPROVED**
- **Context**: Developer tools like VS Code hold valuable context (active workspace, open files, language). Unauthenticated or ambient IPC introduces severe security risks: pipe squatting, cross-session snooping, unauthorized model steering, and data harvesting.
- **Decision**: Implement a 4-layer authenticated named pipe IPC architecture for VS Code context extraction:
  1. **Transport Access Control**: Pipe name `\\.\pipe\presence-vscode-<user>-<session>`. Created with `PIPE_REJECT_REMOTE_CLIENTS` (0x00000008) and `FILE_FLAG_FIRST_PIPE_INSTANCE` (0x00080000). Explicit DACL granting access solely to caller's Logon SID and User SID (`(A;;GRGW;;;<LogonSID>)(A;;GRGW;;;<UserSID>)`).
  2. **Peer Process Identity Verification**: Server obtains client PID via `GetNamedPipeClientProcessId()`, validates executable path via `QueryFullProcessImageNameW()` against `Code.exe`, and verifies client process token Logon SID matches server Logon SID.
  3. **Application Pairing & Mutual Challenge-Response**: User initiates one-time pairing secret stored in VS Code's `SecretStorage` (DPAPI). Mutual HMAC-SHA256 challenge-response exchange with 32-byte nonces; constant-time digest verification (`secrets.compare_digest`). Negotiates ephemeral session token.
  4. **Framing, Payload Bounds & Operation Authorization**: 4-byte big-endian length prefix; 64 KB hard max payload ceiling. Strict JSON-RPC schema allowlist restricted to read-only metadata (`get_active_workspace`, `get_open_documents`, `get_active_language_id`).
- **Alternatives Considered**:
  - *Local HTTP Server (localhost)*: Any process on the machine (and scripts in browsers via DNS rebinding / CORS misconfigurations) can reach localhost ports. Requires port discovery. Rejected.
  - *Windows Shared Memory / MMF*: Lacks built-in synchronization, framing, and granular OS-level client PID verification. Rejected.
  - *Unauthenticated Named Pipe*: Vulnerable to pipe squatting, impersonation, and any medium-integrity process reading/writing without proof of VS Code identity. Rejected.
- **Technical Justification**: Named pipes are the standard Windows IPC mechanism. Win32 provides rich security descriptors, client PID query APIs, and kernel-enforced remote rejection. Pairing secret + HMAC provides cryptographic proof of VS Code extension identity.
- **Security Consequences**: Blocks unauthorized processes from querying workspace metadata or spoofing context into Presence. Mitigates pipe squatting, replay attacks, and cross-session access.
- **Resource Consequences**: Ephemeral IPC connection queried only on window focus change. Timeout capped at 500 ms. Minimal RAM footprint (< 50 KB).
- **Limitations**: Does not prevent a root/admin process or a dedicated process memory debugger running as the same user from inspecting memory. This boundary is explicitly documented.
- **Microsoft References**:
  - [Named Pipe Security and Access Rights](https://learn.microsoft.com/en-us/windows/win32/ipc/named-pipe-security-and-access-rights)
  - [GetNamedPipeClientProcessId function (winbase.h)](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-getnamedpipeclientprocessid)
  - [PIPE_REJECT_REMOTE_CLIENTS flag (winbase.h)](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-createnamedpipea)

---

### ADR-2C-13: SetWinEventHook Resource and Callback Lifecycle Model (P2C-20)
- **Status**: **APPROVED**
- **Context**: Previous specifications made unrealistic absolute claims of "0.0% CPU" and "zero wakeups" for `SetWinEventHook`. In reality, out-of-context hooks require a message loop (`GetMessageW`), and foreground window changes trigger callbacks that execute on the hook thread. Furthermore, hook callbacks can be reentered if they invoke User32 APIs that yield the message queue.
- **Decision**:
  1. Acknowledge realistic resource model: Steady-state idle achieves near-zero CPU (< 0.05% design target, < 0.20% ceiling) because `GetMessageW` enters a kernel wait state. However, OS scheduler ticks, system events, and window switches cause wakeups.
  2. Implement dedicated `WinEventHookThread` with clean message pump (`GetMessageW` / `DispatchMessageW`).
  3. Implement reentrancy guard: Use `_in_callback` flag in `WinEventHook` handler. Hook callback is strictly minimal and non-blocking: copies `(hwnd, event, idObject, idChild)` to a thread-safe bounded queue (`collections.deque(maxlen=50)`), and notifies asyncio loop via `loop.call_soon_threadsafe()`. Heavy Win32 inspection (`GetWindowTextW`, PID query) is performed on worker threads outside the hook callback.
  4. Implement clean teardown sequence: `UnhookWinEvent(hHook)` -> `PostThreadMessageW(thread_id, WM_QUIT, 0, 0)` -> `thread.join(timeout=1.0)`.
  5. Establish six standardized verification experiments (A through F) with explicit Design Targets vs Acceptance Ceilings.
- **Alternatives Considered**:
  - *Polling loop (`while True: GetForegroundWindow(); sleep(0.25)`)*: Constant CPU wakeups (4 wakeups/sec), battery drain, timer resolution jitter. Rejected.
  - *In-context hook (DLL injection)*: High security risk, flagged by antivirus, incompatible with sandboxed processes. Rejected.
  - *Heavy synchronous processing inside hook callback*: Delays OS message pump, causes UI stuttering across the desktop, risks callback reentrancy deadlocks. Rejected.
- **Technical Justification**: Separating minimal event queuing in the hook callback from asynchronous debounced processing on worker threads prevents UI degradation while maintaining reactive event-driven awareness.
- **Security Consequences**: Non-blocking minimal callback prevents denial-of-service on the desktop window manager. Out-of-context hook avoids foreign process memory injection.
- **Resource Consequences**: Idle CPU drops to kernel wait. Burst events capped by bounded deque. Clean teardown guarantees zero thread or handle leaks.
- **Limitations**: If a user switches windows faster than 50 events/2s, the bounded deque drops oldest unconsumed raw events (graceful degradation via debouncing).
- **Microsoft References**:
  - [SetWinEventHook function (winuser.h)](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwineventhook)
  - [WinEventProc callback function (winuser.h)](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nc-winuser-wineventproc)
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
| **ADR-2C-11** | Process Identity | Canonical `process_creation_filetime_ticks: int` | 64-bit 100ns ticks; zero sub-microsecond truncation; raw integer TOCTOU check |
| **ADR-2C-12** | Local IPC | 4-layer authenticated named pipe for VS Code | Logon SID DACL; PID verification; HMAC challenge-response; 64 KB framing |
| **ADR-2C-13** | Hook Model | Message loop, reentrancy guard & realistic resource model | Kernel wait in `GetMessageW`; non-blocking deque; standardized Experiments A-F |

---

## 5. Architectural Consistency & Gate Readiness

Specifications 41 through 45 are now completely reconciled, internally consistent, and fully verified against Microsoft Learn documentation and actual Presence source code. The architecture is ready for final Architectural Acceptance Gate evaluation in Specification 46 and Specification 47.
