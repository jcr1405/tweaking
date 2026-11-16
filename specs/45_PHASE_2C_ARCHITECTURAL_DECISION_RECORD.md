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

## 4. Summary of Architectural Decisions Table

| Decision ID | Area | Decision | Primary Technical Justification |
|---|---|---|---|
| **ADR-2C-01** | Context Acquisition | Native Win32 `SetWinEventHook` + `GetWindowTextW` | Event-driven kernel notification; 0% idle CPU; cached caption |
| **ADR-2C-02** | Threading | Dedicated `WinEventHookThread` | Isolates blocking Win32 message pump from asyncio loop |
| **ADR-2C-03** | Storage | Transient single active snapshot (RAM only) | Zero database bloat; privacy preservation; no ambient history |
| **ADR-2C-04** | Capabilities | Native tools in `ToolRegistry` (`AuthorityLevel`) | Single execution path; unified PermissionBoundary; exact enums |
| **ADR-2C-05** | Window Focus | Best-effort activation + `WindowIdentity` guard | Eliminates `AttachThreadInput` deadlocks; prevents HWND recycling TOCTOU |
| **ADR-2C-06** | Prompt Injection | Structural untrusted data + PermissionBoundary | Natural language injection immunity; delimiters are only formatting |
| **ADR-2C-07** | Shell Safety | Remove `SEE_MASK_NOZONECHECKS`; separate Shell/Job | Preserves security zones; accurate containment boundaries |
| **ADR-2C-08** | Clipboard | Defer clipboard access from default Phase 2C | Eliminates credential harvesting; realistic memory zeroization |
| **ADR-2C-09** | Terminology | Job Objects = Process Lifecycle Containment | Accurate Windows OS security taxonomy (not a security sandbox) |

---

## 5. Architectural Consistency & Gate Readiness

Specifications 41 through 45 are now completely reconciled, internally consistent, and fully verified against Microsoft Learn documentation and actual Presence source code. The architecture is ready for final Architectural Acceptance Gate evaluation in Specification 46.
