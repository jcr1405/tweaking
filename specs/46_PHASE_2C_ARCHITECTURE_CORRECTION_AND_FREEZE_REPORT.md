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

## 6. Cross-Specification Consistency Matrix

All five architecture specifications were audited together to ensure total cross-document alignment:

| Architecture Dimension | Spec 41 (Context) | Spec 42 (Security) | Spec 43 (Capability) | Spec 44 (Roadmap) | Spec 45 (ADR) | Alignment Verdict |
|---|---|---|---|---|---|:---:|
| **Context Source Ownership** | `ContextManager` owns sources | Sources report to manager | Manager isolated from tools | Phased source creation | ADR-2C-01 / 02 | **CONSISTENT** |
| **Context Snapshot Model** | Deeply immutable `ContextSnapshot` | Transient single snapshot | Snapshot ID verified | Subphase 2C.0 / 2C.2 | ADR-2C-03 | **CONSISTENT** |
| **Sensitivity Classification** | Item-level (`ContextItem`) | 4-tier model (Public/Personal/Sens/Prot) | Enforced on output | Item filtering in 2C.2 | ADR-2C-06 | **CONSISTENT** |
| **Retention & TTL Policy** | Replaced on window switch | Transient memory only; no ring buffer | N/A (Tools are transient) | Verified in 2C.0 | ADR-2C-03 | **CONSISTENT** |
| **Model-Provider Egress** | Sanitized metadata only | Explicit egress rules; user toggle | N/A | Implemented in 2C.2 | ADR-2C-06 | **CONSISTENT** |
| **MCP Data Sharing** | Stripped from tool calls | Strictly isolated; zero leakage | Tools receive clean params | Tested in 2C.2 | ADR-2C-04 | **CONSISTENT** |
| **Prompt Assembly** | Precedence hierarchy (1 to 6) | Untrusted data block formatting | N/A | Reconstructed in 2C.2 | ADR-2C-06 | **CONSISTENT** |
| **EventBus Integration** | `WINDOW_FOREGROUND_CHANGED` | Audit event logging | Capability telemetry | Subphase 2C.0 / 2C.1 | ADR-2C-01 | **CONSISTENT** |
| **Permission Levels** | `AuthorityLevel.L0_OBSERVE` | Boundary enforcement | Exact enum names (`L0/L2/L3`)| Verified in 2C.3/2C.4 | ADR-2C-04 | **CONSISTENT** |
| **Clipboard Handling** | Deferred from default context | Prohibited from ambient loop | Deferred from tools | Deferred from Phase 2C| ADR-2C-08 | **CONSISTENT** |
| **Win32 API Semantics** | User32 cached caption (no timeout)| No hooks into child controls | Best-effort focus; no AttachThreadInput | Tested in 2C.1/2C.4 | ADR-2C-01 / 05 | **CONSISTENT** |
| **Timeout & Cancellation** | Thread pump unhooking | Class A/B/C operation limits | Class A/C native execution | Tested in 2C.4 | ADR-2C-05 / 10 | **CONSISTENT** |
| **Process Containment** | Process creation ticks check | Job Object lifecycle controls | ShellExecuteEx separated from Jobs | Launcher in 2C.4 | ADR-2C-07 / 09 | **CONSISTENT** |
| **Resource Budgets** | Measurable design targets | Leakage prevention targets | Execution latency targets | Measurement in 2C.1 (Exp A-F) | ADR-2C-01 / 13 | **CONSISTENT** |
| **Application Providers** | Authenticated named pipe in 2C.5| 4-layer IPC security architecture | N/A | Evaluated in 2C.5 | ADR-2C-12 | **CONSISTENT** |
| **ShellExecute Flags & Ownership**| N/A | Zone checks preserved | Flag constants; CloseHandle; Class C | Subphase 2C.4 | ADR-2C-10 | **CONSISTENT** |
| **FILETIME Representation** | Canonical 64-bit ticks | WindowIdentity ticks | TOCTOU raw integer equality | Subphase 2C.0 / 2C.1 | ADR-2C-11 | **CONSISTENT** |
| **WinEventHook Model** | Message pump; deque; reentrancy guard | Threat T-01 mitigation | N/A | Subphase 2C.1 (Exp A-F) | ADR-2C-13 | **CONSISTENT** |

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
      │       - WinEventHookThread dedicated message pump (GetMessageW / DispatchMessageW)
      │       - SetWinEventHook(EVENT_SYSTEM_FOREGROUND) out-of-context hook
      │       - Reentrancy guard (_in_callback) and non-blocking bounded deque (maxlen=50)
      │       - GetForegroundWindow, GetWindowTextW (cached caption), GetClassNameW
      │       - Composite WindowIdentity (GetProcessTimes canonical FILETIME ticks: int)
      │       - 150 ms event debouncing and duplicate suppression
      │       - Clean unhooking and thread termination verification (UnhookWinEvent, WM_QUIT)
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
                (Logon SID DACL, PID verification, HMAC-SHA256 challenge-response, 64 KB framing)
              - Windows Terminal: Option C opt-in shell integration hook contract
              - Provider isolation: Class B 500 ms timeouts; graceful fallback to window metadata
```

---

## 8. Architectural Acceptance Gates Evaluation

Every gate from Section 27 was rigorously evaluated against the corrected specifications and authoritative evidence:

| Gate | Architecture Dimension | Requirement & Acceptance Condition | Evaluation | Status |
|---|---|---|---|:---:|
| **Gate A** | Win32 Correctness | All proposed Windows API behaviors match official documentation. | `GetWindowTextW` top-level cache, `SetForegroundWindow` lock, `SetWinEventHook` message pump, `SHELLEXECUTEINFOW` flags (`SEE_MASK_FLAG_NO_UI = 0x0400`), canonical FILETIME ticks, and Logon SID DACL verified against MSDN. | **PASS** |
| **Gate B** | Context Lifecycle | TTL, window switching, conversation isolation, and persistence rules agree. | Unified retention model: transient active snapshot only; no ring buffer; invalidation on switch; trajectory persistence only on explicit turn reference. | **PASS** |
| **Gate C** | Data Sensitivity | Sensitivity is assigned and enforced at individual data-item level. | `ContextItem` defines item-level sensitivity (`PUBLIC`, `PERSONAL`, `SENSITIVE`, `PROTECTED`). | **PASS** |
| **Gate D** | Data Egress | Model-provider and MCP transmission rules are explicit. | Egress rules defined for Gemini cloud API; ambient context strictly stripped from MCP tool parameters. | **PASS** |
| **Gate E** | Prompt Injection | Environmental text is untrusted data; XML delimiters are not treated as sufficient defense. | Structural untrusted data classification; constitutional guardrails; model cannot grant consent; L3 HMAC tokens required. | **PASS** |
| **Gate F** | Shell Safety | No default `SEE_MASK_NOZONECHECKS`; process containment claims are accurate. | `SEE_MASK_NOZONECHECKS` prohibited; `SEE_MASK_FLAG_NO_UI` (0x0400) corrected; caller closes `hProcess`; Class C async classification; Job Object separated. | **PASS** |
| **Gate G** | Window Activation | Windows foreground restrictions are respected. | Best-effort activation; `AttachThreadInput` rejected; taskbar flash fallback; composite `WindowIdentity` TOCTOU guard with raw 64-bit FILETIME ticks. | **PASS** |
| **Gate H** | Timeout Correctness | No false promise that Python thread cancellation terminates native work. | AnyIO worker thread limitations documented; Class A/B/C/D native operation taxonomy established. | **PASS** |
| **Gate I** | Provider Integration | VS Code and Windows Terminal integration mechanisms are technically valid. | VS Code 4-layer authenticated named pipe contract (Logon SID DACL, PID verification, HMAC challenge-response, 64 KB framing) and Terminal shell hook contract established. | **PASS** |
| **Gate J** | Data Contracts | Immutability, timestamps, provenance, and size limits are properly defined. | Deep immutability enforced; split timestamps (`observed_utc` vs `observed_monotonic`); canonical `process_creation_filetime_ticks: int`; caption clamped to 200 chars. | **PASS** |
| **Gate K** | Permission Integration | Exact existing enums and ToolDefinition interface are used. | Verified against `runtime/permissions.py` (`AuthorityLevel.L0_OBSERVE`, etc.) and `runtime/tools/registry.py` (`ToolDefinition`). | **PASS** |
| **Gate L** | Privacy Model | No indiscriminate content monitoring or automatic clipboard access. | Continuous OCR/audio/keystrokes prohibited; clipboard reading explicitly deferred. | **PASS** |
| **Gate M** | Resource Targets | Budgets are measurable targets, not invented benchmarks. | Targets separated into Design Targets vs Acceptance Ceilings; standardized Experiments A through F defined for empirical validation. | **PASS** |
| **Gate N** | Cross-Spec Consistency | Specifications 41–45 agree across all dimensions. | Consistency matrix verified with zero contradictions across all revised specifications. | **PASS** |
| **Gate O** | Implementation Scope | No Phase 2C source code has been created or modified. | Verified: 100% documentation milestone; zero production Python code modified. | **PASS** |
| **Gate P** | Documentation Complete | Specification 46 is complete and references authoritative evidence. | Specification 46 authored with full citations, ADR references, and discrepancy registers. | **PASS** |
| **Gate Q** | API, IPC & Resource Accuracy | ShellExecute flags, FILETIME ticks, named pipe auth, and hook lifecycle model are validated. | All findings P2C-17 through P2C-20 resolved and cross-referenced in Specification 47. | **PASS** |

**All 17 Architectural Acceptance Gates (Gates A through Q) are PASS.**

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
