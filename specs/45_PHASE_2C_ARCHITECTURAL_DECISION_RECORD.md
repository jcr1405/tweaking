# SPECIFICATION 45 — PHASE 2C: ARCHITECTURAL DECISION RECORD (ADR)
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

### ADR-2C-03: Transient In-Memory Context Snapshot vs SQLite Persistence
- **Status**: **APPROVED**
- **Context**: Should every observed window change be written to the SQLite database?
- **Decision**: **No.** Environmental context is transient and held strictly in volatile RAM (active `ContextSnapshot` + 20-entry ring buffer). Context is written to SQLite **only** when an active `Step` in an executed agent turn explicitly references or acts upon that context.
- **Consequences**:
  - *Positive*: Prevents unbounded SQLite database growth from normal user window switching.
  - *Positive*: Protects user privacy: ambient window history is wiped completely when Presence exits.
  - *Positive*: SQLite trajectory schema (`runtime/trajectory.py`) remains 100% frozen with zero migration needed.

---

### ADR-2C-04: Capability Integration through Existing `ToolRegistry` and `PermissionBoundary`
- **Status**: **APPROVED**
- **Context**: When introducing native Windows capabilities (e.g. `desktop.activate_window`, `shell.open_path`), should a new "capability execution engine" be created?
- **Decision**: **No.** Native capabilities must register as standard tools in `ToolRegistry` and enforce authorization through `PermissionBoundary`.
- **Consequences**:
  - *Positive*: Preserves single unified execution path: `AgentExecutor -> ToolRegistry -> PermissionBoundary`.
  - *Positive*: Automatically gains existing multi-tier security: `L0_OBSERVATION` for read-only metadata, `L2_EXECUTION` for desktop actions, and `L3_PROTECTED` with HMAC `ConsentToken` for sensitive data (clipboard).
  - *Positive*: Full audit logging in trajectory steps is inherited automatically.

---

### ADR-2C-05: Reversible Window Activation over Synthetic Input Injection
- **Status**: **APPROVED**
- **Context**: Should Presence support synthetic mouse clicks and keyboard typing (`SendInput`) to automate arbitrary desktop applications in Phase 2C?
- **Decision**: **Reject synthetic input injection.** Phase 2C capabilities are restricted to clean, reversible desktop actions: window activation (`SetForegroundWindow`), shell opening (`ShellExecuteExW`), and application launching inside Win32 Job Objects.
- **Consequences**:
  - *Positive*: Synthetic input injection is notoriously brittle, coordinates drift with screen scaling, and missed clicks cause catastrophic accidental data deletion.
  - *Positive*: Reversible window activation allows Presence to bring relevant windows to the user's attention safely without usurping user mouse control.

---

### ADR-2C-06: Defensive XML Envelopes for Untrusted Environmental Text
- **Status**: **APPROVED**
- **Context**: Window titles and UI text observed from third-party applications (web browsers, emails, documents) can contain malicious indirect prompt injection payloads designed to deceive the model.
- **Decision**: Wrap all environmental text inside `<environmental_context status="observed" untrusted="true">` blocks, sanitize control characters, truncate at 200 characters, and prepend explicit constitutional untrusted-data warnings.
- **Consequences**:
  - *Positive*: Establishes structural defense-in-depth against prompt injection.
  - *Positive*: The model is trained to treat enclosed text as metadata rather than instructions.
  - *Positive*: Even if the model attempts an action, destructive tools are gated by `PermissionBoundary` L3 consent tokens.

---

### ADR-2C-07: In-Process Application Context Providers with Strict Timeout Isolation
- **Status**: **APPROVED**
- **Context**: How should application-specific context (e.g. VS Code workspace, Windows Terminal directory) be queried?
- **Decision**: Implement lightweight in-process provider adapters that query application state through existing interfaces (window title heuristics, CLI status queries, or local IPC) wrapped in strict **500 ms timeouts**.
- **Consequences**:
  - *Positive*: Avoids running background daemon processes for each supported application.
  - *Positive*: Timeouts prevent a slow or frozen application from stalling Presence.
  - *Positive*: Failure of an application provider fails gracefully to generic window metadata without crashing the runtime.

---

## 4. Summary of Architecture Decisions Table

| Decision ID | Area | Decision | Primary Justification |
|---|---|---|---|
| **ADR-2C-01** | Context Acquisition | Native Win32 `SetWinEventHook` | Event-driven kernel notification; 0% idle CPU |
| **ADR-2C-02** | Threading | Dedicated `WinEventHookThread` | Isolates blocking Win32 message pump from asyncio loop |
| **ADR-2C-03** | Storage | Volatile RAM Ring Buffer | Zero database bloat; privacy preservation |
| **ADR-2C-04** | Capabilities | Native tools in `ToolRegistry` | Single execution path; unified PermissionBoundary |
| **ADR-2C-05** | Automation | Window activation (No `SendInput`) | Brittle synthetic clicks rejected; focus safety |
| **ADR-2C-06** | Security | Defensive XML untrusted delimiters | Indirect prompt injection defense-in-depth |
| **ADR-2C-07** | App Providers | In-process adapters with 500ms timeout | Low overhead; hang immunity |

---

## 5. Architectural Consistency & Gate Readiness

Specifications 41 through 45 form a completely unified, coherent, and defensible architectural definition for Phase 2C. 

No contradictions exist between data contracts (Spec 41), privacy constraints (Spec 42), capability contracts (Spec 43), implementation subphases (Spec 44), and technology selection records (Spec 45). The architecture is ready for final Architectural Acceptance Gate evaluation.
