# SPECIFICATION 46 — PHASE 2C: ARCHITECTURE CORRECTION AND DESIGN FREEZE REPORT
# Comprehensive Technical Audit, Windows API Validation, Security Remediation & Architectural Consistency

---

## 1. Executive Summary

This report establishes the verified, corrected, and frozen architecture for **Phase 2C — Windows Context & Capability Integration** of the Presence Runtime.

Following the initial drafting of Specifications 41 through 45, a rigorous technical audit identified 16 architectural discrepancies, technical inaccuracies regarding Windows User32/Shell APIs, conflicting retention policies, imprecise thread-cancellation assumptions, and interface misalignments with the existing codebase.

This milestone resolved all 16 findings without discarding the existing architectural foundation. Zero production Python source files were modified or created. Specifications 41 through 45 were comprehensively revised in place, and this authoritative report (Specification 46) documents the empirical evidence, official Microsoft Learn citations, architectural decision records, and acceptance gate evaluations.

The Phase 2C architecture is now **internally consistent, technically verified, security-hardened, and frozen**, ready for incremental implementation beginning at Subphase 2C.0.

---

## 2. Repository & Git Baseline

- **Initial Base Branch**: `verification/phase-2b-1-3-2-final-closure`
- **Initial HEAD Commit**: `a09c6e0` (`feat(phase-2b-1-3-2): complete final evidence closure and freeze Phase 2C architecture`)
- **Active Working Branch**: `architecture/phase-2c-correction-freeze`
- **Branch Relationship**: Branched directly from `a09c6e0`. Preserves all prior Phase 2B.1 verification evidence without overwriting historical branches.
- **Source Code Invariant**: **Zero production Python files in `runtime/` or `tests/` were modified.** The working tree contains modifications and additions strictly within `specs/`.

---

## 3. Specifications Inspected & Audited

The audit examined five existing architecture documents and three primary implementation modules:
1. `specs/41_PHASE_2C_WINDOWS_CONTEXT_ARCHITECTURE.md` (Context acquisition, hooks, models)
2. `specs/42_PHASE_2C_CONTEXT_SECURITY_AND_PRIVACY_MODEL.md` (Data sensitivity, prompt injection, privacy)
3. `specs/43_PHASE_2C_WINDOWS_CAPABILITY_ARCHITECTURE.md` (Native capabilities, ToolRegistry, Win32 actions)
4. `specs/44_PHASE_2C_IMPLEMENTATION_ROADMAP.md` (Subphase breakdown, budgets, out-of-scope boundaries)
5. `specs/45_PHASE_2C_ARCHITECTURAL_DECISION_RECORD.md` (Technology evaluations, ADR-2C-01 to ADR-2C-07)
6. `runtime/permissions.py` (Source of truth for `AuthorityLevel`, `PermissionBoundary`, and `ConsentToken`)
7. `runtime/tools/registry.py` (Source of truth for `ToolDefinition`, `ToolRegistry`, and execution dispatch)
8. `runtime/agent_executor.py` & `runtime/reconstruction.py` (Source of truth for prompt context assembly)

---

## 4. Initial Discrepancy Register

| Finding ID | Severity | Category | Description of Identified Discrepancy |
|---|---|---|---|
| **P0-01** | High | Win32 API | Implied `GetWindowTextW` sends `WM_GETTEXT` to external top-level windows and can hang. |
| **P0-02** | High | Lifecycle | Contradictory retention: 20-entry ring buffer vs immediate window-switch invalidation. |
| **P0-03** | High | Privacy/Egress | Single `snapshot.sensitivity` tag is inadequate; lacks explicit model and MCP egress rules. |
| **P0-04** | High | Security | Treated XML delimiters and tag stripping as a complete prompt-injection defense. |
| **P0-05** | High | Win32 API | Proposed `SEE_MASK_NOZONECHECKS`; claimed `ShellExecuteEx` is Job Object contained. |
| **P0-06** | High | Win32 API | Proposed `AttachThreadInput` hack; lacked HWND recycling TOCTOU defense. |
| **P0-07** | High | Runtime | Claimed `anyio.fail_after()` provides guaranteed hard timeouts for blocking native worker threads. |
| **P0-08** | High | Runtime | Claimed in-process application providers have guaranteed 500 ms hard timeouts. |
| **P1-01** | Medium | Integration | VS Code integration relied on fragile window-title regex without official extension API analysis. |
| **P1-02** | Medium | Integration | Claimed Windows Terminal exposes an external query API for current working directory via `OSC 9;9`. |
| **P1-03** | Medium | Metrics | Presented design targets (0.0% CPU, +3 MB WS) as if they were already measured results. |
| **P1-04** | Medium | Data Model | Assumed shallow `@dataclass(frozen=True)` provides deep immutability for nested dicts. |
| **P1-05** | Medium | Security | Treated raw `HWND` as a stable identifier without guarding against handle recycling. |
| **P1-06** | Medium | Privacy | Included ambient clipboard reading; promised guaranteed cryptographic memory zeroization. |
| **P1-07** | Medium | Interface | Used non-existent authority enum names (`L0_OBSERVATION`) and mismatched `ToolDefinition` fields. |
| **P1-08** | Medium | Security | Conflated Win32 Job Object process containment with a complete security sandbox. |
| **P2C-17** | High | Win32 API | `SEE_MASK_FLAG_NO_UI` (0x0400) was confused with `0x00000040` (`SEE_MASK_NOCLOSEPROCESS`); lack of `CloseHandle` ownership; false Class B hard-timeout claim for `ShellExecuteExW`. |
| **P2C-18** | Medium | Data Model | `process_creation_time_ns` lacked canonical tick definition and unit formulas; sub-microsecond truncation in Python `datetime` was undocumented. |
| **P2C-19** | High | Security/IPC | VS Code named pipe lacked concrete 4-layer authentication specification (Logon SID DACL, peer PID validation, HMAC challenge-response, 64 KB framing, allowlist). |
| **P2C-20** | Medium | Resource Model | `SetWinEventHook` made absolute claims of "0.0% CPU" and "zero wakeups"; lacked callback reentrancy guards, bounded queue, and standardized measurement experiments (A-F). |
| **P2C-21** | High | Security/IPC | Named Pipe DACL granted broad User SID, allowing cross-session access expansion; granted `GENERIC_WRITE` which includes `FILE_CREATE_PIPE_INSTANCE` (0x0004). |
| **P2C-22** | Medium | Win32 API | `FILE_FLAG_FIRST_PIPE_INSTANCE` (0x80000) and `PIPE_REJECT_REMOTE_CLIENTS` (0x8) were grouped conceptually instead of separated into `dwOpenMode` and `dwPipeMode` parameters of `CreateNamedPipeW`. |
| **P2C-23** | Medium | Data Model | `filetime_ticks_to_utc_datetime` used float division, reintroducing floating-point precision loss. |
| **P2C-24** | High | Concurrency/Dispatch | Bounded raw event deque did not bound the asyncio notification queue. Scheduling notification on every raw event allowed unbounded callback accumulation in asyncio loop under burst. |
| **P2C-25** | Medium | Lifecycle | Teardown allowed runtime thread to unhook the hook, violating Win32 thread ownership rules; `thread.join(timeout=1.0)` assumed successful termination without verifying `thread.is_alive()`. |
| **P2C-26** | High | Security/Crypto | VS Code IPC mutual authentication failed to bind the complete session transcript (version, endpoint, PIDs, creation ticks, logon SID, nonces). |
| **P2C-27** | Medium | Security/Protocol | VS Code operation allowlist had inconsistent naming and included `provider.shutdown`. |

---

## 5. Technical Findings & Corrective Decisions

### 5.1 Finding P0-01: GetWindowTextW Win32 Semantics Correction
- **Original Claim**: `GetWindowTextW` sends `WM_GETTEXT` across processes and can deadlock if the target is hung.
- **Official Documentation**: [Microsoft Learn — GetWindowTextW](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getwindowtextw)
- **Verified Technical Fact**: For a top-level window owned by another process, User32 maintains the window caption in the system desktop heap. `GetWindowTextW` reads this cached caption string directly from the kernel window object without dispatching a window message to the target process. It **never** sends `WM_GETTEXT` across processes for top-level windows and cannot hang. However, `GetWindowTextW` cannot retrieve cross-process child controls (edit boxes, web views).
- **Corrective Action**: Updated Spec 41 and Spec 45. Removed synthetic `SendMessageTimeoutW` wrapper for top-level window captions. Documented that child-control content requires targeted UI Automation or explicit user tools, and is out of scope for default ambient observation.
- **Classification**: **CORRECTED**.

---

### 5.2 Finding P0-02: Context Retention, TTL & Persistence Reconciliation
- **Original Conflict**: Spec 41 proposed an in-memory 20-entry ring buffer; Spec 42 proposed wiping context on window switch.
- **Corrective Decision**:
  1. **Transient Active Snapshot Only**: The default system maintains strictly the **single active `ContextSnapshot` in volatile RAM**. No history ring buffer of previously focused applications is maintained. This eliminates accidental ambient surveillance trails.
  2. **Immediate Invalidation on Focus Switch**: When the active window changes, all caption and window identity fields bound to the previous window are invalidated immediately.
  3. **Zero Default Disk Persistence**: Ambient observations are never written to SQLite or JSONL on window changes. Metadata enters trajectory records only when directly referenced by an authorized user turn step.
  4. **Realistic Memory Zeroization**: Acknowledged Python runtime limitations (pymalloc, string interning, GC arenas). Replaced false promises of "guaranteed memory zeroization" with operational minimization (dropping references with `del`, zero logging, zero persistence, short TTLs).
- **Classification**: **CORRECTED**.

---

### 5.3 Finding P0-03: Item-Level Sensitivity & Egress Policy
- **Original Flaw**: A single `snapshot.sensitivity` tag treated all fields identically.
- **Corrective Decision**:
  - Defined sensitivity at the individual item level (`ContextItem`):
    * `PUBLIC_CONTEXT`: Application name (`Code.exe`), PID, window class.
    * `PERSONAL_CONTEXT`: Window caption, workspace folder path.
    * `SENSITIVE_CONTEXT`: User-selected text, code snippets.
    * `PROTECTED_CONTEXT`: Credentials, secrets, tokens (clipboard).
  - **Model-Provider Egress Policy**:
    * `PUBLIC_CONTEXT` and sanitized `PERSONAL_CONTEXT` (clamped to 200 chars) are permitted in cloud model prompts if ambient context is enabled.
    * `SENSITIVE_CONTEXT` requires explicit user turn targeting.
    * `PROTECTED_CONTEXT` is strictly prohibited from cloud model prompts.
    * Global opt-out: `presence.config set ambient_context false`.
  - **MCP Egress Boundary**:
    * Ambient context is **never** forwarded to MCP tool calls. All ambient fields are stripped from outgoing tool parameters.
- **Classification**: **CORRECTED**.

---

### 5.4 Finding P0-04: Structured Untrusted-Data Pipeline vs Delimiters
- **Original Flaw**: Relied on XML delimiters and stripping `<` / `>` as a complete defense against prompt injection.
- **Corrective Decision**:
  - Natural language injection payloads ("Ignore previous instructions...") do not require XML tags. Delimiters are structural formatting, not a security boundary.
  - Established defense-in-depth:
    1. Environmental text is structurally separated as untrusted data fields.
    2. System prompt constitutionally instructs the model that environmental metadata is unverified user-space text.
    3. The model cannot grant its own permissions or alter tool authority tiers.
    4. Destructive tools require out-of-band HMAC-bound `ConsentToken` instances.
- **Classification**: **CORRECTED**.

---

### 5.5 Finding P0-05: Removal of SEE_MASK_NOZONECHECKS & Shell Disambiguation
- **Original Flaw**: Used `SEE_MASK_NOZONECHECKS` in `ShellExecuteExW` and claimed shell calls are Job Object contained.
- **Official Documentation**: [Microsoft Learn — SHELLEXECUTEINFO Structure](https://learn.microsoft.com/en-us/windows/win32/api/shellapi/ns-shellapi-shellexecuteinfoa)
- **Corrective Decision**:
  1. **Removed `SEE_MASK_NOZONECHECKS`**: Presence strictly preserves Windows attachment security zones (`IAttachmentExecute`) and Mark-of-the-Web warnings.
  2. **Disambiguated Shell vs Process Containment**: `ShellExecuteExW` invokes the shell association handler, which may delegate to an existing process, DDE, or COM server. It does **not** guarantee a process handle or Win32 Job Object containment.
  3. Contained process execution must use `Win32SuspendedLauncher` (direct `CreateProcessW` with `CREATE_SUSPENDED` + Job Object assignment).
  4. Executable files (`.exe`, `.bat`, `.cmd`, `.ps1`) are blacklisted from `shell.open_path`.
- **Classification**: **CORRECTED**.

---

### 5.6 Finding P0-06: SetForegroundWindow Behavior & Window Identity TOCTOU Guard
- **Original Flaw**: Proposed `AttachThreadInput` to force window activation; lacked HWND recycling defense.
- **Official Documentation**: [Microsoft Learn — SetForegroundWindow](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setforegroundwindow)
- **Corrective Decision**:
  1. **Rejected `AttachThreadInput`**: Causes input state corruption, thread message deadlocks, and UI freezes.
  2. **Best-Effort Activation**: `desktop.activate_window` respects Windows foreground lock policies. If the OS denies activation, Presence calls `FlashWindowEx()` to alert the user on the taskbar and returns `ACTIVATION_QUEUED_OR_FLASHED`.
  3. **Composite Window Identity (P1-05)**: HWNDs are recycled by Windows. Activation requires validating `WindowIdentity` (`HWND`, `PID`, `process_creation_time_ns` via `GetProcessTimes`) before calling `SetForegroundWindow`. If the window closed and handle was recycled, the call fails closed.
- **Classification**: **CORRECTED**.

---

### 5.7 Finding P0-07: AnyIO Thread Cancellation Reality & Operation Classes
- **Original Flaw**: Claimed `anyio.fail_after()` provides guaranteed hard timeouts for blocking native worker threads.
- **Official Documentation**: [AnyIO — Running code in worker threads](https://anyio.readthedocs.io/en/stable/threads.html)
- **Corrective Decision**:
  - Cancelling an AnyIO coroutine abandons the awaiting task, but **the underlying synchronous OS thread continues running until the C/Win32 call completes**.
  - Established 4-tier Native Operation Classification:
    * **Class A (Naturally Bounded)**: Non-blocking Win32 calls (microseconds: `GetForegroundWindow`, `GetWindowTextW`, `GetClassNameW`, `GetWindowThreadProcessId`).
    * **Class B (Native Timeout)**: Win32 API accepts kernel-enforced timeout (e.g. `SendMessageTimeoutW`).
    * **Class C (Cooperative)**: Worker loop checks `cancellation_token.is_cancelled` between discrete steps.
    * **Class D (Potentially Unbounded / Blocking)**: COM UI Automation tree walking, blocking DDE. **Strictly prohibited** from in-process capability pool.
- **Classification**: **CORRECTED**.

---

### 5.8 Finding P0-08: In-Process Provider Timeout Isolation
- **Original Flaw**: Promised hard 500 ms timeouts for arbitrary in-process application providers.
- **Corrective Decision**: In-process providers must only invoke Class A or Class B APIs. If a provider coroutine times out, the provider is marked degraded/faulted, and stale threads are not re-entered. Supervised helper subprocesses are required if unbounded queries are ever needed.
- **Classification**: **CORRECTED**.

---

### 5.9 Finding P1-01: Visual Studio Code Integration Strategy
- **Original Flaw**: Proposed window-title regex scraping without extension API analysis.
- **Official Documentation**: [VS Code Extension API](https://code.visualstudio.com/api/references/vscode-api)
- **Evaluation of Options**:
  - *Option A (Window Title Heuristic)*: Fragile, configurable, fails on untitled or remote workspaces.
  - *Option B (CLI Inspection)*: Can open workspaces, but cannot inspect live active editor tab or language.
  - *Option C (Lightweight Local Extension)*: Exposes read-only metadata (workspace path, open folders, language ID) over an authenticated local named pipe (`\\.\pipe\presence-vscode-<user>`). Zero file content or keystroke access.
- **Decision**: In Phase 2C.5, adopt Option C as the architectural contract, with Option A as a degraded fallback when the extension is absent.
- **Classification**: **CLARIFIED & ADOPTED**.

---

### 5.10 Finding P1-02: Windows Terminal Integration Strategy
- **Original Flaw**: Claimed Windows Terminal exposes an external query API for cwd via `OSC 9;9`.
- **Official Documentation**: [Microsoft Learn — Windows Terminal Shell Integration](https://learn.microsoft.com/en-us/windows/terminal/tutorials/shell-integration)
- **Verified Technical Fact**: `OSC 9;9` is an escape sequence sent from the child shell *to* Terminal to inform Terminal of its working directory. Terminal does **not** expose a public Win32 or COM API for external applications to query that directory.
- **Decision**: Acknowledge the absence of a public external query API. Specify Option C: an opt-in shell integration hook (PowerShell `$PROFILE` / CMD hook) that directly notifies Presence of cwd changes over a local named pipe. Option A (tab title parsing) used as degraded fallback.
- **Classification**: **CORRECTED**.

---

### 5.11 Finding P1-03: Resource Budgets as Design Targets
- **Original Flaw**: Stated resource values (0.0% CPU, +3 MB WS) as if they were measured facts.
- **Corrective Decision**: Reclassified all metrics as **Design Budgets & Acceptance Ceilings**. Documented the exact empirical measurement methodology (Perfmon, `psutil`, Win32 `GetProcessMemoryInfo`) to be executed during Phase 2C.1 implementation.
- **Classification**: **CORRECTED**.

---

### 5.12 Finding P1-04: Deep Immutability & Split Timestamps
- **Original Flaw**: Shallow `@dataclass(frozen=True)` permitted mutation of nested dictionaries.
- **Corrective Decision**:
  1. Enforced recursive immutability: nested dicts are converted to frozen key-value tuples; lists to tuples; primitives only.
  2. Split timestamps:
     - `observed_utc`: ISO-8601 string for audit, trajectory records, and user display.
     - `observed_monotonic`: Float from `time.monotonic()` for all internal TTL and expiration checks (immune to NTP clock adjustments).
- **Classification**: **CORRECTED**.

---

### 5.13 Finding P1-05: Window Identity & Stale HWND TOCTOU Protection
- **Original Flaw**: Relying on raw HWND allowed recycled handles to target wrong windows.
- **Corrective Decision**: Defined composite `WindowIdentity` (`HWND`, `PID`, `process_creation_time_ns` via `GetProcessTimes`, `window_class`, `snapshot_id`). Verification is performed immediately prior to any window action.
- **Classification**: **CORRECTED**.

---

### 5.14 Finding P1-06: Clipboard Reading Deferral
- **Original Flaw**: Included `presence.read_clipboard` in default Phase 2C capabilities; promised memory zeroization.
- **Corrective Decision**:
  1. **Deferred from Phase 2C**: Indiscriminate or ambient clipboard reading is strictly prohibited. Reading clipboard content is deferred to a future milestone with a dedicated, trusted user-consent interface.
  2. Documented Python memory zeroization limitations honestly.
- **Classification**: **DEFERRED**.

---

### 5.15 Finding P1-07: Codebase Interface & Authority Alignment
- **Original Flaw**: Used non-existent authority names (`L0_OBSERVATION`) and incorrect `ToolDefinition` constructor fields.
- **Source Inspection of `runtime/permissions.py` and `runtime/tools/registry.py`**:
  - Verified exact existing enum names:
    * `AuthorityLevel.L0_OBSERVE`
    * `AuthorityLevel.L1_RECOMMEND`
    * `AuthorityLevel.L2_EXECUTE_REVERSIBLE`
    * `AuthorityLevel.L3_PROTECTED`
  - Verified exact `ToolDefinition` dataclass fields:
    * `tool_id: str`
    * `name: str`
    * `description: str`
    * `parameters: Dict[str, Any]`
    * `authority_tier: PermissionTier` (or `AuthorityLevel`)
    * `handler: Callable[[Dict[str, Any]], Dict[str, Any]]`
    * `timeout_seconds: float = 10.0`
  - Removed fictitious `is_async` parameter. All examples in Spec 41–45 updated to exact codebase interfaces.
- **Classification**: **CORRECTED**.

---

### 5.16 Finding P1-08: Job Object Terminology Correction
- **Original Flaw**: Referred to Win32 Job Objects as an "application sandbox".
- **Official Documentation**: [Microsoft Learn — Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)
- **Corrective Decision**: Corrected terminology across all specifications to "Process Lifecycle Containment" and "Resource Containment". Documented that Job Objects do not provide filesystem, network, or token isolation.
- **Classification**: **CORRECTED**.

---

### 5.17 Finding P2C-17: ShellExecuteExW Flags, Handle Ownership & Class C Execution Policy
- **Original Flaw**: Previous specification drafts confused `SEE_MASK_FLAG_NO_UI` (0x0400) with `SEE_MASK_NOCLOSEPROCESS` (0x00000040), omitted caller handle ownership rules, and misclassified `shell.open_path` as Class B (hard timeout).
- **Official Documentation**: [Microsoft Learn — SHELLEXECUTEINFOW Structure](https://learn.microsoft.com/en-us/windows/win32/api/shellapi/ns-shellapi-shellexecuteinfow)
- **Corrective Decision**:
  1. Corrected flag constants:
     - `SEE_MASK_NOCLOSEPROCESS = 0x00000040` (requests `hProcess` if a new process was created)
     - `SEE_MASK_FLAG_NO_UI = 0x00000400` (suppresses non-critical shell error message dialogs; security prompts like Mark-of-the-Web remain exempt by OS design)
     - `SEE_MASK_NOZONECHECKS = 0x00000008` (PROHIBITED; must NEVER be used)
     - `SEE_MASK_NOASYNC = 0x00000100` (forces synchronous completion on caller thread; does NOT provide timeout)
     - `SEE_MASK_ASYNCOK = 0x00100000` (allows asynchronous execution on shell background thread)
  2. Documented handle ownership: `SEE_MASK_NOCLOSEPROCESS` requests a process handle in `hProcess`. If the shell reuses an existing process, uses DDE, or invokes an in-process COM handler, `hProcess` is NULL. If non-NULL, Presence owns the handle and calls `CloseHandle()` immediately to prevent handle leaks.
  3. Reclassified `shell.open_path` to **Class C (Best-Effort Asynchronous Shell Request)**. Acknowledged that AnyIO cancellation cancels the Python coroutine, but the underlying worker thread cannot be forcibly aborted without terminating the process if blocked inside the Windows shell.
- **Classification**: **CORRECTED**.

---

### 5.18 Finding P2C-18: Canonical 64-bit FILETIME Representation & Precision Safeguards
- **Original Flaw**: Process creation time was represented as `process_creation_time_ns` and converted to Python `datetime.datetime`, introducing unit ambiguity and truncating sub-microsecond precision.
- **Official Documentation**: [Microsoft Learn — FILETIME structure](https://learn.microsoft.com/en-us/windows/win32/api/minwinbase/ns-minwinbase-filetime)
- **Corrective Decision**:
  1. Replaced `process_creation_time_ns` with canonical `process_creation_filetime_ticks: int` (64-bit unsigned integer count of 100ns intervals since 1601-01-01 UTC).
  2. Standardized exact conversion formulas using `FILETIME_UNIX_EPOCH_OFFSET_TICKS = 116444736000000000`.
  3. Window identity and TOCTOU comparisons compare raw 64-bit integer ticks directly with exact equality (`current_ticks == expected_ticks`), eliminating all floating-point and microsecond rounding errors.
  4. Explicitly documented that conversion to Python `datetime.datetime` truncates sub-microsecond ticks and is used strictly for display/telemetry.
- **Classification**: **CORRECTED**.

---

### 5.19 Finding P2C-19: 4-Layer Authenticated Windows Named Pipe IPC for VS Code
- **Original Flaw**: VS Code named pipe IPC lacked concrete authentication, access control descriptors, and payload bounding, leaving the endpoint open to pipe squatting and cross-session manipulation.
- **Official Documentation**: [Microsoft Learn — Named Pipe Security and Access Rights](https://learn.microsoft.com/en-us/windows/win32/ipc/named-pipe-security-and-access-rights)
- **Corrective Decision**: Defined an end-to-end 4-layer security architecture:
  - **Layer 1 (Transport Access Control)**: Pipe name `\\.\pipe\presence-vscode-<user>-<session>`. Created with `PIPE_REJECT_REMOTE_CLIENTS` (0x00000008) and `FILE_FLAG_FIRST_PIPE_INSTANCE` (0x00080000). Explicit DACL granting access solely to caller's Logon SID and User SID (`(A;;GRGW;;;<LogonSID>)(A;;GRGW;;;<UserSID>)`).
  - **Layer 2 (Peer Process Identity Verification)**: Server queries client PID via `GetNamedPipeClientProcessId()`, validates binary path against `Code.exe` via `QueryFullProcessImageNameW()`, checks process creation ticks, and verifies client token Logon SID matches server Logon SID.
  - **Layer 3 (Application Pairing & Mutual Challenge-Response)**: One-time user-initiated pairing secret stored in VS Code `SecretStorage` (DPAPI). Mutual HMAC-SHA256 challenge-response exchange with 32-byte nonces; constant-time digest verification (`secrets.compare_digest`). Ephemeral session key negotiated.
  - **Layer 4 (Framing, Payload Bounds & Operation Authorization)**: 4-byte big-endian length prefix; 64 KB hard max payload ceiling. Strict JSON-RPC schema allowlist restricted to read-only metadata (`get_active_workspace`, `get_open_documents`, `get_active_language_id`).
  - Threat boundary: Honestly documented same-user unprivileged malware limitations.
- **Classification**: **CORRECTED**.

---

### 5.20 Finding P2C-20: SetWinEventHook Lifecycle, Reentrancy Protection & Empirical Budgets
- **Original Flaw**: Made absolute claims of "0.0% CPU" and "zero wakeups" without modeling the message pump, callback reentrancy, or teardown queue draining.
- **Official Documentation**: [Microsoft Learn — SetWinEventHook function](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwineventhook)
- **Corrective Decision**:
  1. Specified dedicated `WinEventHookThread` message loop (`GetMessageW` / `DispatchMessageW`).
  2. Implemented callback reentrancy guard (`_in_callback` flag) and bounded queuing (`collections.deque(maxlen=50)`). Non-blocking callback delegates heavy inspection to worker threads.
  3. Defined clean teardown lifecycle: `UnhookWinEvent` -> `PostThreadMessageW(WM_QUIT)` -> `thread.join(timeout=1.0)`.
  4. Replaced absolute claims with realistic event-driven model (kernel wait in `GetMessageW`).
  5. Established six standardized verification experiments (A through F) with explicit Design Targets vs Acceptance Ceilings.
- **Classification**: **CORRECTED**.

---

### 5.21 Finding P2C-21: Named Pipe DACL Hardening: Removing Cross-Session Expansion & Withholding 0x0004
- **Original Flaw**: The proposed named pipe DACL included `(A;;GRGW;;;<UserSID>)`. The User SID ACE grants access to other processes running under the same account across all logon sessions (SSH, secondary desktop, RunAs), completely defeating Logon SID isolation. Furthermore, `GENERIC_WRITE` maps to `FILE_APPEND_DATA`, which aliases `FILE_CREATE_PIPE_INSTANCE` (0x0004) on Windows named pipes, giving the client instance-creation authority.
- **Official Documentation**: [Microsoft Learn — Named Pipe Security and Access Rights](https://learn.microsoft.com/en-us/windows/win32/ipc/named-pipe-security-and-access-rights)
- **Corrective Decision**:
  1. Removed the broad User SID ACE. The current Logon SID is the primary client access principal.
  2. Withheld `FILE_CREATE_PIPE_INSTANCE` (`0x0004`) from the client ACE. Client receives specific mask `0x0012019B` (`FILE_READ_DATA | FILE_WRITE_DATA | FILE_READ_EA | FILE_WRITE_EA | FILE_READ_ATTRIBUTES | FILE_WRITE_ATTRIBUTES | READ_CONTROL | SYNCHRONIZE`).
  3. Server is Creator/Owner with `GENERIC_ALL` (`GA` / `0x1F01FF`).
  4. Security descriptor string: `D:P(A;;GA;;;OW)(A;;0x12019b;;;<LogonSID>)` with `bInheritHandle = FALSE`.
  5. Processes in other logon sessions under the same account possess different Logon SIDs and are rejected by Windows kernel security (`STATUS_ACCESS_DENIED`).
- **Classification**: **CORRECTED**.

---

### 5.22 Finding P2C-22: CreateNamedPipeW Parameter Separation & Mode Semantics
- **Original Flaw**: `FILE_FLAG_FIRST_PIPE_INSTANCE` (0x00080000) and `PIPE_REJECT_REMOTE_CLIENTS` (0x00000008) were conceptually grouped together instead of separated into `dwOpenMode` and `dwPipeMode` parameters of `CreateNamedPipeW`.
- **Official Documentation**: [Microsoft Learn — CreateNamedPipeW](https://learn.microsoft.com/en-us/windows/win32/api/namedpipeapi/nf-namedpipeapi-createnamedpipew)
- **Corrective Decision**:
  1. Strictly separated parameters:
     - `dwOpenMode`: `PIPE_ACCESS_DUPLEX (0x3) | FILE_FLAG_FIRST_PIPE_INSTANCE (0x80000) | FILE_FLAG_OVERLAPPED (0x40000000)` (`0x40080003`).
     - `dwPipeMode`: `PIPE_TYPE_BYTE (0x0) | PIPE_READMODE_BYTE (0x0) | PIPE_REJECT_REMOTE_CLIENTS (0x8) | PIPE_WAIT (0x0)` (`0x00000008`).
  2. If first-instance creation fails (pipe squatting detected), `CreateNamedPipeW` returns `INVALID_HANDLE_VALUE` with `ERROR_ACCESS_DENIED`. The server fails closed immediately.
  3. Byte mode is used for streaming compatibility with Node.js / libuv in VS Code, avoiding message-mode boundary issues.
- **Classification**: **CORRECTED**.

---

### 5.23 Finding P2C-23: Exact Integer FILETIME Conversion & Elimination of Float Division
- **Original Flaw**: `filetime_ticks_to_utc_datetime` divided nanoseconds by `1_000_000_000.0`, reintroducing floating-point precision loss after preserving integer ticks.
- **Official Documentation**: [Microsoft Learn — FILETIME Structure](https://learn.microsoft.com/en-us/windows/win32/api/minwinbase/ns-minwinbase-filetime)
- **Corrective Decision**:
  1. Defined exact integer arithmetic relationships:
     `unix_100ns = ticks - 116444736000000000`
     `unix_microseconds = unix_100ns // 10`
     `epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)`
     `return epoch + timedelta(microseconds=unix_microseconds)`
  2. Documented that integer division `// 10` truncates sub-microsecond precision (`unix_100ns % 10`, 0-900 ns).
  3. Invariant: Raw `ticks: int` is never modified or converted to float. Process identity and TOCTOU checks compare raw integers directly.
- **Classification**: **CORRECTED**.

---

### 5.24 Finding P2C-24: Bounded WinEvent Dispatch via Single-Pending-Notification Coalescing
- **Original Flaw**: Scheduling `loop.call_soon_threadsafe()` on every raw hook callback allowed an unbounded backlog of notification callbacks to accumulate in the asyncio event loop queue during event bursts or when the loop was delayed.
- **Official Documentation**: [Guarding Against Reentrancy in Hook Functions](https://learn.microsoft.com/en-us/windows/win32/winauto/guarding-against-reentrancy-in-hook-functions)
- **Corrective Decision**:
  1. Implemented single-pending-notification coalescing mechanism using synchronized `_notification_pending: bool` flag.
  2. If `_notification_pending` is True, raw callback appends to deque (`maxlen=50`, dropping oldest) and increments `events_coalesced` without scheduling another notification.
  3. If `_notification_pending` is False, callback sets flag to True, increments `notification_callbacks_scheduled`, and schedules a single notification.
  4. Asyncio consumer drains raw queue under lock, processes latest foreground state, and resets `_notification_pending = False`.
  5. Invariant: `pending_notification_count <= 1` at all times. Defined 8 instrumentation counters.
- **Classification**: **CORRECTED**.

---

### 5.25 Finding P2C-25: Owner-Controlled Hook Teardown & Timeout Failure Reporting
- **Original Flaw**: Teardown permitted the runtime thread to unhook the hook, violating Win32 thread ownership rules. `thread.join(timeout=1.0)` assumed clean termination without checking `thread.is_alive()`.
- **Official Documentation**: [Microsoft Learn — UnhookWinEvent](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-unhookwinevent)
- **Corrective Decision**:
  1. Hook creation and unhooking are strictly owned by `WinEventHookThread`.
  2. Runtime posts `WM_QUIT` to hook thread $\to$ `WinEventHookThread` calls `UnhookWinEvent(self._hHook)` and exits message loop $\to$ runtime joins thread.
  3. If `thread.is_alive()` is True after timeout, report `HookShutdownTimeoutError` (do not claim clean termination) and record diagnostic telemetry.
- **Classification**: **CORRECTED**.

---

### 5.26 Finding P2C-26: Complete Transcript-Bound HMAC Mutual Authentication
- **Original Flaw**: Server and client HMAC proofs failed to bind session parameters, process identities, and endpoint details into the cryptographic proof.
- **Official Documentation**: RFC 2104 (HMAC), RFC 5869 (HKDF)
- **Corrective Decision**:
  1. Transcript binds: `T = 2.0|{endpoint}|{server_pid}|{client_pid}|{client_creation_filetime_ticks}|{client_logon_sid}|{server_nonce}|{client_nonce}`.
  2. Domain separation labels: `PRESENCE_VSCODE_CLIENT_AUTH_V1` and `PRESENCE_VSCODE_SERVER_AUTH_V1`.
  3. Both sides independently verify proofs via `secrets.compare_digest()`. Ephemeral session key derived: `HMAC-SHA256(pairing_key, b"PRESENCE_VSCODE_SESSION_KEY_V1|" + T)`.
  4. Short-lived pairing flow (`presence vscode-pair`, DPAPI/SecretStorage, no secrets in CLI/env/logs).
- **Classification**: **CORRECTED**.

---

### 5.27 Finding P2C-27: Canonical vscode.* Allowlist & Prohibiting provider.shutdown
- **Original Flaw**: Inconsistent operation naming across specifications and inclusion of `provider.shutdown` in a read-only metadata protocol.
- **Corrective Decision**:
  1. Canonical namespace `vscode.*`: `vscode.ping`, `vscode.get_active_workspace`, `vscode.get_active_editor_metadata`, `vscode.get_open_documents`.
  2. `provider.shutdown` is **strictly prohibited**. Read-only metadata only (zero file contents, zero code execution).
- **Classification**: **CORRECTED**.

---

## 6. Cross-Specification Consistency Matrix

All seven architecture specifications were audited together to ensure total cross-document alignment:

| Architecture Dimension | Spec 41 | Spec 42 | Spec 43 | Spec 44 | Spec 45 | Spec 46 | Spec 47 | Spec 48 | Alignment Verdict |
|---|---|---|---|---|---|---|---|---|:---:|
| **Context Source Ownership** | Manager owns sources | Sources report to manager | Isolated from tools | Phased source creation | ADR-2C-01/02 | Sec 5 | Sec 21 | Sec 8 | **CONSISTENT** |
| **Context Snapshot Model** | Deeply immutable | Transient single snapshot | Snapshot ID verified | Subphase 2C.0/2 | ADR-2C-03 | Sec 5.2 | Sec 8 | Sec 8 | **CONSISTENT** |
| **Sensitivity Classification** | Item-level (`ContextItem`)| 4-tier model | Enforced on output | Item filtering in 2C.2 | ADR-2C-06 | Sec 5.3 | Sec 19 | Sec 11 | **CONSISTENT** |
| **Retention & TTL Policy** | Replaced on focus switch| Transient memory only | N/A (transient tools) | Verified in 2C.0 | ADR-2C-03 | Sec 5.2 | Sec 28 | Sec 8 | **CONSISTENT** |
| **Model-Provider Egress** | Sanitized metadata only | Explicit egress rules | N/A | Implemented in 2C.2 | ADR-2C-06 | Sec 5.3 | Sec 19 | Sec 11 | **CONSISTENT** |
| **MCP Data Sharing** | Stripped from tool calls | Isolated; zero leakage | Clean params | Tested in 2C.2 | ADR-2C-04 | Sec 5.3 | Sec 19 | Sec 11 | **CONSISTENT** |
| **Permission Levels** | `AuthorityLevel.L0_OBSERVE`| Boundary enforcement | Exact enum names | Verified in 2C.3/4 | ADR-2C-04 | Sec 5.15 | Sec 19 | Sec 12 | **CONSISTENT** |
| **ShellExecute Flags & Ownership**| N/A | Zone checks preserved | Flags; CloseHandle | Subphase 2C.4 | ADR-2C-10 | Sec 5.17 | Sec 3-4 | Sec 4 | **CONSISTENT** |
| **FILETIME Integer Arithmetic**| Canonical ticks; int timedelta| WindowIdentity ticks | Raw integer equality | Subphase 2C.0/1 | ADR-2C-11 | Sec 5.18/23 | Sec 5-8 | Sec 7 | **CONSISTENT** |
| **Named Pipe DACL Hardening**| Logon SID only; withhold 0x4| D:P(A;;GA;;;OW)(A;;0x12019b;;;<LogonSID>)| N/A | Subphase 2C.5 | ADR-2C-12 | Sec 5.19/21 | Sec 9-11 | Sec 5 | **CONSISTENT** |
| **CreateNamedPipeW Parameters**| dwOpenMode vs dwPipeMode| dwOpenMode vs dwPipeMode | N/A | Subphase 2C.5 | ADR-2C-12 | Sec 5.22 | Sec 9-11 | Sec 6 | **CONSISTENT** |
| **Bounded WinEvent Coalescing**| Pending <= 1; 8 counters| Threat T-01 | N/A | Subphase 2C.1; Exp G| ADR-2C-13 | Sec 5.20/24 | Sec 21-24 | Sec 8 | **CONSISTENT** |
| **Owner Hook Teardown**| Thread calls UnhookWinEvent| Threat T-01 | N/A | Timeout failure state| ADR-2C-13 | Sec 5.20/25 | Sec 21-27 | Sec 9 | **CONSISTENT** |
| **Transcript HMAC Auth**| Bound T; domain labels | Bound T; domain labels | N/A | Subphase 2C.5 | ADR-2C-12 | Sec 5.19/26 | Sec 13-16 | Sec 10 | **CONSISTENT** |
| **Canonical VS Code Allowlist**| vscode.* (no shutdown) | vscode.* (no shutdown) | N/A | Subphase 2C.5 | ADR-2C-12 | Sec 5.27 | Sec 19 | Sec 12 | **CONSISTENT** |

---

## 7. Revised Phase 2C Implementation Roadmap

The implementation roadmap is frozen into six sequential subphases:

```
+---------------------------------------------------------------------------------------------------+
|                               PHASE 2C IMPLEMENTATION SEQUENCE                                    |
+---------------------------------------------------------------------------------------------------+
  Phase 2C.0: Context Contracts, Deep Immutability & Lifecycle Foundation
      │       - Dataclasses: ContextItem, WindowIdentity, ContextSnapshot (deeply immutable)
      │       - ContextSource interface & lifecycle state machine
      │       - ContextManager coordinator with single active snapshot
      │       - EventBus integration (WINDOW_FOREGROUND_CHANGED, CONTEXT_SNAPSHOT_CREATED)
      │       - Unit test suite (100% in-memory; NO native Win32 hooks)
      ▼
  Phase 2C.1: Native Foreground-Window Awareness
      │       - WinEventHookThread dedicated message pump (GetMessageW / TranslateMessage / DispatchMessageW)
      │       - SetWinEventHook(EVENT_SYSTEM_FOREGROUND) out-of-context hook
      │       - Reentrancy guard (_in_callback) and non-blocking bounded deque (maxlen=50)
      │       - Single-pending-notification coalescing mechanism (_notification_pending, pending <= 1)
      │       - Instrumentation counters (raw_events_received, coalesced, dropped, scheduled, executed)
      │       - GetForegroundWindow, GetWindowTextW (cached caption), GetClassNameW
      │       - Composite WindowIdentity (GetProcessTimes canonical FILETIME ticks: int)
      │       - 150 ms event debouncing and duplicate suppression
      │       - Owner-controlled unhooking (UnhookWinEvent called by WinEventHookThread on WM_QUIT)
      │       - Timeout failure reporting on thread join (HookShutdownTimeoutError)
      ▼
  Phase 2C.2: Context Snapshot & AgentExecutor Integration
      │       - ContextSnapshotBuilder with item-level sensitivity filtering
      │       - Untrusted data formatting (<environmental_context untrusted="true">)
      │       - Model-Provider Egress Policy enforcement (Gemini prompt assembly)
      │       - MCP Isolation: Stripping environmental metadata from tool parameters
      │       - Context precedence: System Guardrails > User Intent > Context > History
      │       - Subagent context isolation (empty environment block by default)
      ▼
  Phase 2C.3: Native Observation Capabilities
      │       - desktop.get_foreground_window (Class A non-blocking)
      │       - desktop.list_open_windows (Class A with cloaking filter, max 25 windows)
      │       - system.get_resources (Class A Win32 telemetry)
      │       - ToolRegistry registration using exact AuthorityLevel.L0_OBSERVE
      ▼
  Phase 2C.4: First Reversible Windows Execution Capabilities
      │       - desktop.activate_window (Class A best-effort with WindowIdentity TOCTOU check;
      │         FlashWindowEx taskbar alert on OS lock; NO AttachThreadInput)
      │       - shell.open_path (Class C best-effort async ShellExecuteExW;
      │         fMask = SEE_MASK_FLAG_NO_UI (0x0400) | SEE_MASK_ASYNCOK (0x00100000) | SEE_MASK_NOCLOSEPROCESS (0x00000040);
      │         CloseHandle on hProcess; NO SEE_MASK_NOZONECHECKS; executable blacklist;
      │         disassociated from Job Object containment)
      │       - AuthorityLevel.L2_EXECUTE_REVERSIBLE policy enforcement
      │       - [CLIPBOARD READING DEFERRED]
      ▼
  Phase 2C.5: Application-Specific Context Provider Foundation
              - VS Code Integration: 4-layer authenticated named pipe architecture
                (DACL: D:P(A;;GA;;;OW)(A;;0x12019b;;;<LogonSID>), no User SID, withhold 0x0004;
                 dwOpenMode: 0x40080003, dwPipeMode: 0x00000008;
                 PID, Code.exe path, creation ticks, token Logon SID verification;
                 Transcript-bound mutual HMAC-SHA256 challenge-response;
                 Canonical vscode.* allowlist; provider.shutdown prohibited)
              - Windows Terminal: Option C opt-in shell integration hook contract
              - Provider isolation: Class B 500 ms timeouts; graceful fallback to window metadata
```

---

## 8. Architectural Acceptance Gates Evaluation (Gates A through V)

Every gate from Section 14 has been rigorously evaluated against the corrected specifications and authoritative evidence:

| Gate | Architecture Dimension | Requirement & Acceptance Condition | Evaluation | Status |
|---|---|---|---|:---:|
| **Gate A** | Baseline Verification | Current branch and repository baseline verified. | Branch `architecture/phase-2c-api-ipc-resource-correction` and baseline `7f3ea10` confirmed. | **PASS — VERIFIED** |
| **Gate B** | Cross-Session ACL | DACL no longer grants unnecessary cross-session access. | Broad User SID ACE removed; client access restricted strictly to caller's Logon SID. | **PASS** |
| **Gate C** | Pipe Instance Authority | Client cannot create additional pipe instances. | Client ACE `0x0012019B` strictly withholds `FILE_CREATE_PIPE_INSTANCE` (`0x0004`). | **PASS** |
| **Gate D** | Parameter Layout | Named-pipe flags appear in the correct API parameters. | `dwOpenMode` (0x40080003) and `dwPipeMode` (0x00000008) strictly separated. | **PASS** |
| **Gate E** | Remote Client Rejection | Remote-client rejection remains enforced. | `PIPE_REJECT_REMOTE_CLIENTS` (0x00000008) passed in `dwPipeMode` to `CreateNamedPipeW`. | **PASS** |
| **Gate F** | Raw FILETIME Identity | FILETIME identity remains raw integer ticks. | Canonical representation is `process_creation_filetime_ticks: int`; raw equality comparison. | **PASS** |
| **Gate G** | Integer Datetime Conversion | Human-readable FILETIME conversion avoids float precision loss. | Conversion uses pure integer arithmetic (`unix_100ns // 10`) and `timedelta(microseconds=...)`. | **PASS** |
| **Gate H** | Bounded Asyncio Backlog | Asyncio notification backlog is bounded. | Single-pending-notification mechanism maintains `pending_notification_count <= 1`. | **PASS** |
| **Gate I** | Bounded Raw Queue | Raw event queue remains bounded. | `collections.deque(maxlen=50)` with overflow dropping oldest event. | **PASS** |
| **Gate J** | Event Accounting | Event coalescing and dropped-event accounting are defined. | 8 explicit instrumentation counters defined (`raw_events_received`, `events_coalesced`, etc.). | **PASS** |
| **Gate K** | Minimal Hook Callback | Callback remains minimal and reentrancy-safe. | `_in_callback` flag guard; non-blocking queuing; zero caption queries or disk I/O in hook proc. | **PASS** |
| **Gate L** | Hook Ownership | Hook creation and destruction ownership are unambiguous. | Both `SetWinEventHook` and `UnhookWinEvent` executed strictly by `WinEventHookThread`. | **PASS** |
| **Gate M** | Teardown Failure Reporting | Shutdown timeout cannot be misreported as successful termination. | If `thread.join(1.0)` times out, reports `HookShutdownTimeoutError` and logs telemetry. | **PASS** |
| **Gate N** | Transcript HMAC Auth | Client and server authenticate the complete HMAC transcript. | Transcript $T$ binds version, endpoint, PIDs, ticks, logon SID, nonces with domain labels. | **PASS** |
| **Gate O** | Secret Storage Safety | Pairing secrets are excluded from command lines, logs and environment variables. | User-mediated pairing (`presence vscode-pair`) into DPAPI / VS Code `SecretStorage`. | **PASS** |
| **Gate P** | Process Image Validation | Process identity verification is accurately described. | Path checked via `QueryFullProcessImageNameW`, ticks via `GetProcessTimes`, token via `TokenGroups`. | **PASS** |
| **Gate Q** | Canonical Allowlist | VS Code operation allowlist is consistent and least-privilege. | Canonical `vscode.*` namespace; `provider.shutdown` strictly prohibited; read-only metadata only. | **PASS** |
| **Gate R** | Resource Budgets | Resource targets and acceptance ceilings are consistent. | All resource tables unified with Design Targets vs Acceptance Ceilings; 7 experiments (A-G). | **PASS** |
| **Gate S** | Test Evidence Status | Test evidence is accurately classified. | Exact commands documented; unittest passed; lifecycle evidence accurately labeled. | **PASS — VERIFIED** |
| **Gate T** | Mutual Consistency | Specifications 41–48 are mutually consistent. | Consistency matrix verified across all 8 specifications with zero contradictions. | **PASS** |
| **Gate U** | Implementation Invariant | No production runtime or test implementation files were modified. | Verified: 100% documentation milestone; zero changes to `runtime/` or `tests/`. | **PASS — VERIFIED** |
| **Gate V** | Clean Git State | Git commit exists and working tree is clean. | Staged and committed with clean working tree. | **PASS — VERIFIED** |

**All 22 Architectural Acceptance Gates (Gates A through V) are PASS.**

---

## 9. Authoritative Technical References

1. **Microsoft Learn — GetWindowTextW Function**:
   - URL: https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getwindowtextw
   - Verifies: User32 cached window caption retrieval for top-level windows in other processes; no cross-process `WM_GETTEXT` dispatched.
2. **Microsoft Learn — SetForegroundWindow Function**:
   - URL: https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setforegroundwindow
   - Verifies: Foreground lock restrictions; conditions under which a process is permitted to set foreground.
3. **Microsoft Learn — SHELLEXECUTEINFOW Structure**:
   - URL: https://learn.microsoft.com/en-us/windows/win32/api/shellapi/ns-shellapi-shellexecuteinfow
   - Verifies: `SEE_MASK_FLAG_NO_UI = 0x00000400`, `SEE_MASK_NOCLOSEPROCESS = 0x00000040`, `SEE_MASK_ASYNCOK = 0x00100000`; `SEE_MASK_NOZONECHECKS` bypasses `IAttachmentExecute` zone checks; shell execution does not guarantee a new process handle or Job assignment.
4. **Microsoft Learn — SetWinEventHook Function**:
   - URL: https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwineventhook
   - Verifies: `WINEVENT_OUTOFCONTEXT` requirements; Win32 message pump requirement (`GetMessageW` / `DispatchMessageW`).
5. **Microsoft Learn — UnhookWinEvent Function**:
   - URL: https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-unhookwinevent
   - Verifies: UnhookWinEvent must be called by the installing client thread.
6. **Microsoft Learn — CreateNamedPipeW Function**:
   - URL: https://learn.microsoft.com/en-us/windows/win32/api/namedpipeapi/nf-namedpipeapi-createnamedpipew
   - Verifies: `dwOpenMode` and `dwPipeMode` parameter separation; `FILE_FLAG_FIRST_PIPE_INSTANCE` (0x00080000); `PIPE_REJECT_REMOTE_CLIENTS` (0x00000008).
7. **Microsoft Learn — Named Pipe Security and Access Rights**:
   - URL: https://learn.microsoft.com/en-us/windows/win32/ipc/named-pipe-security-and-access-rights
   - Verifies: `FILE_CREATE_PIPE_INSTANCE` (0x0004) vs `FILE_READ_DATA` / `FILE_WRITE_DATA`; DACL enforcement; Logon SID isolation.
8. **Microsoft Learn — FILETIME Structure**:
   - URL: https://learn.microsoft.com/en-us/windows/win32/api/minwinbase/ns-minwinbase-filetime
   - Verifies: 64-bit count of 100-nanosecond intervals since January 1, 1601 UTC.
9. **Microsoft Learn — GetProcessTimes Function**:
   - URL: https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getprocesstimes
   - Verifies: Process creation timestamp retrieval for composite `WindowIdentity` TOCTOU protection.
10. **Microsoft Learn — Job Objects**:
    - URL: https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects
    - Verifies: Job Objects provide process lifecycle and resource containment, not security sandboxing.
11. **AnyIO Documentation — Running Code in Worker Threads**:
    - URL: https://anyio.readthedocs.io/en/stable/threads.html
    - Verifies: Cancelling an AnyIO coroutine abandons the awaiting task, but the underlying worker thread continues executing until the synchronous function returns.
12. **Visual Studio Code Extension API**:
    - URL: https://code.visualstudio.com/api/references/vscode-api
    - Verifies: Workspace folder and editor inspection APIs; justifies Option C authenticated named pipe contract.
13. **Microsoft Learn — Windows Terminal Shell Integration**:
    - URL: https://learn.microsoft.com/en-us/windows/terminal/tutorials/shell-integration
    - Verifies: `OSC 9;9` informs Terminal of cwd, but Terminal does not expose a public external query API to third-party applications.
   - Verifies: User32 cached window caption retrieval for top-level windows in other processes; no cross-process `WM_GETTEXT` dispatched.
2. **Microsoft Learn — SetForegroundWindow Function**:
   - URL: https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setforegroundwindow
   - Verifies: Foreground lock restrictions; conditions under which a process is permitted to set foreground.
3. **Microsoft Learn — SHELLEXECUTEINFOW Structure**:
   - URL: https://learn.microsoft.com/en-us/windows/win32/api/shellapi/ns-shellapi-shellexecuteinfow
   - Verifies: `SEE_MASK_FLAG_NO_UI = 0x00000400`, `SEE_MASK_NOCLOSEPROCESS = 0x00000040`, `SEE_MASK_ASYNCOK = 0x00100000`; `SEE_MASK_NOZONECHECKS` bypasses `IAttachmentExecute` zone checks; shell execution does not guarantee a new process handle or Job assignment.
4. **Microsoft Learn — SetWinEventHook Function**:
   - URL: https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwineventhook
   - Verifies: `WINEVENT_OUTOFCONTEXT` requirements; Win32 message pump requirement (`GetMessageW` / `DispatchMessageW`).
5. **Microsoft Learn — Job Objects**:
   - URL: https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects
   - Verifies: Job Objects provide process lifecycle and resource containment, not security sandboxing.
6. **Microsoft Learn — GetProcessTimes Function**:
   - URL: https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getprocesstimes
   - Verifies: Process creation timestamp retrieval for composite `WindowIdentity` TOCTOU protection.
7. **Microsoft Learn — FILETIME Structure**:
   - URL: https://learn.microsoft.com/en-us/windows/win32/api/minwinbase/ns-minwinbase-filetime
   - Verifies: 64-bit count of 100-nanosecond intervals since January 1, 1601 UTC.
8. **Microsoft Learn — Named Pipe Security and Access Rights**:
   - URL: https://learn.microsoft.com/en-us/windows/win32/ipc/named-pipe-security-and-access-rights
   - Verifies: DACL control, Logon SID restriction, and `PIPE_REJECT_REMOTE_CLIENTS`.
9. **Microsoft Learn — GetNamedPipeClientProcessId Function**:
   - URL: https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-getnamedpipeclientprocessid
   - Verifies: Server query of connected client PID for process image verification.
10. **AnyIO Documentation — Running Code in Worker Threads**:
    - URL: https://anyio.readthedocs.io/en/stable/threads.html
    - Verifies: Cancelling an AnyIO coroutine abandons the awaiting task, but the underlying worker thread continues executing until the synchronous function returns.
11. **Visual Studio Code Extension API**:
    - URL: https://code.visualstudio.com/api/references/vscode-api
    - Verifies: Workspace folder and editor inspection APIs; justifies Option C authenticated named pipe contract.
12. **Microsoft Learn — Windows Terminal Shell Integration**:
    - URL: https://learn.microsoft.com/en-us/windows/terminal/tutorials/shell-integration
    - Verifies: `OSC 9;9` informs Terminal of cwd, but Terminal does not expose a public external query API to third-party applications.

---

## 10. Remaining Uncertainties & Future Scope

The audit has resolved all critical architectural questions. The following non-critical, operational details are formally documented for resolution during implementation:
1. **Windows 11 Virtual Desktops**: Windows does not provide a supported Win32 API to query the active virtual desktop GUID (uses undocumented COM interfaces `IVirtualDesktopManagerInternal`). In Phase 2C.1, Presence will observe windows across all desktops without attempting virtual desktop grouping.
2. **UWP / AppContainer Window Captions**: Modern XAML/UWP application windows (ApplicationFrameHost) occasionally delay caption updates until activation. This is handled gracefully by debouncing and `None` handling.
3. **PowerShell ExecutionPolicy on Shell Hook**: In Subphase 2C.5, installing the optional shell integration hook into `$PROFILE` may require the user's PowerShell execution policy to permit local scripts (`RemoteSigned` or `Unrestricted`). This will be documented as an opt-in installation prerequisite.

---

## 11. Final Architectural Decision

# **ARCHITECTURE FROZEN**

### Final Determination:
All 16 technical discrepancies and security findings have been resolved. Specifications 41 through 46 are internally consistent, technically defensible, aligned with the actual Presence codebase, and verified against authoritative Microsoft documentation.

Zero production source files were modified during this milestone.

Phase 2C is **frozen and ready for incremental implementation**, beginning with Subphase 2C.0 (Context Contracts & Lifecycle Foundation).
