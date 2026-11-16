# SPECIFICATION 44 — PHASE 2C: IMPLEMENTATION ROADMAP (CORRECTED)
# Persistent Intelligent Presence — Phased Implementation Sequence, Dependencies & Verification Gates

## 1. Roadmap Architecture & Phasing Strategy

Phase 2C connects the Presence intelligence backend to the active Windows desktop environment. 

To maintain engineering rigor, prevent regressions, and enforce resource and security discipline, Phase 2C is partitioned into **six discrete, testable subphases (2C.0 through 2C.5)**.

Each subphase has its own explicit scope, dependency boundaries, security gates, resource budgets, and exit criteria. No subphase begins until the prior subphase passes its verification suite.

```
+---------------------------------------------------------------------------------------------------+
|                               PHASE 2C IMPLEMENTATION SEQUENCE                                    |
+---------------------------------------------------------------------------------------------------+
  Phase 2C.0: Context Contracts, Deep Immutability & Lifecycle Foundation
      │       (Models, deep immutability, state machine, EventBus integration, retention policy)
      │       [NO NATIVE HOOKS]
      ▼
  Phase 2C.1: Native Foreground-Window Awareness
      │       (WinEventHookThread, User32 bindings, debouncing, composite WindowIdentity)
      │       [NO SCREENSHOTS, NO OCR, NO KEYLOGGING]
      ▼
  Phase 2C.2: Context Snapshot & AgentExecutor Integration
      │       (Snapshot builder, prompt block formatting, untrusted delimiter injection, egress rules)
      ▼
  Phase 2C.3: Native Observation Capabilities
      │       (ToolRegistry integration, desktop.get_foreground_window, desktop.list_open_windows)
      │       [EXACT AuthorityLevel.L0_OBSERVE, ToolDefinition interface]
      ▼
  Phase 2C.4: First Reversible Windows Execution Capabilities
      │       (Best-effort desktop.activate_window, shell.open_path with zone checks preserved)
      │       [NO SEE_MASK_NOZONECHECKS, NO ATTACHTHREADINPUT, CLIPBOARD DEFERRED]
      ▼
  Phase 2C.5: Application-Specific Context Provider Foundation
              (VS Code authenticated named pipe contract, Terminal shell integration contract)
```

---

## 2. Detailed Subphase Specifications

### 2.1 Subphase 2C.0: Context Contracts & Lifecycle Foundation
- **Objective**: Establish core context data contracts, deep immutability value objects, and state machines without native OS bindings.
- **Scope**:
  - `ContextItem`, `WindowIdentity`, `ContextSnapshot`, `ContextSensitivity` models.
  - Deep immutability enforcement (nested tuples, primitive constraints).
  - Split timestamp architecture (`observed_utc` for audit, `observed_monotonic` for TTL).
  - Canonical `process_creation_filetime_ticks: int` representation (64-bit count of 100ns intervals since 1601-01-01 UTC) and exact conversion formulas.
  - `ContextSource` abstract base class and lifecycle state machine (`UNINITIALIZED -> STARTING -> RUNNING -> STOPPING -> STOPPED`).
  - `ContextManager` coordinator with mock context source.
  - Unified retention policy (transient in-memory active snapshot; no history ring buffer).
  - EventBus integration (`WINDOW_FOREGROUND_CHANGED`, `CONTEXT_SNAPSHOT_CREATED`).
- **Files Created**:
  - `runtime/context/__init__.py`
  - `runtime/context/models.py`
  - `runtime/context/contracts.py`
  - `runtime/context/source.py`
  - `runtime/context/manager.py`
  - `runtime/context/expiration.py`
  - `tests/test_context_foundation.py`
- **Files Modified**:
  - `runtime/events.py` (Declare typed context event dataclasses)
- **Security Gate**: All dataclasses frozen and deeply immutable; item-level sensitivity enforced.
- **Resource Budget**: Design Target < 1 MB memory overhead; Acceptance Ceiling < 2 MB; zero OS threads added.
- **Exit Criteria**: 100% unit test pass on context models, state transitions, TTL expiration, and EventBus dispatches.

---

### 2.2 Subphase 2C.1: Native Foreground-Window Awareness
- **Objective**: Connect `ContextManager` to real Windows desktop events via native Win32 APIs.
- **Scope**:
  - Implement `ForegroundWindowSource` using `SetWinEventHook(EVENT_SYSTEM_FOREGROUND)`.
  - Dedicate `WinEventHookThread` with Win32 message pump (`GetMessageW` / `DispatchMessageW`).
  - Implement hook callback reentrancy guards (`_in_callback` flag, non-blocking callback queuing to `collections.deque(maxlen=50)`, and thread-safe handoff via `loop.call_soon_threadsafe()`).
  - Retrieve top-level window caption via `GetWindowTextW` (User32 cached caption; no synthetic timeouts).
  - Construct composite `WindowIdentity` (`HWND`, `PID`, `process_creation_filetime_ticks: int`, `window_class`).
  - Implement 150 ms event debouncing and duplicate suppression.
  - Guarantee clean unhooking via `UnhookWinEvent`, `WM_QUIT`, and thread join.
- **Files Created**:
  - `runtime/context/sources/__init__.py`
  - `runtime/context/sources/foreground_window.py`
  - `tests/test_win32_foreground_source.py`
  - `tests/adversarial/test_adv_context_debouncing.py`
  - `tests/adversarial/test_adv_hook_reentrancy.py`
- **Files Modified**:
  - `runtime/platform_win32.py` (Add User32/Kernel32 window inspection bindings)
  - `runtime/context/manager.py` (Register `ForegroundWindowSource`)
- **Security Gate**: Out-of-context hook only (no DLL injection); handle isolation verified via `CloseHandle`.
- **Resource Budget**:
  - Thread Count: Exactly 1 background OS thread (`WinEventHookThread`).
  - Idle CPU: Design Target < 0.05%; Acceptance Ceiling < 0.20% (event-driven kernel wait in `GetMessageW`).
  - Working Set Overhead: Design Target < 3.5 MB; Acceptance Ceiling < 8.0 MB.
- **Exit Criteria**: Hook reliably detects window switches; callback reentrancy verified; debouncing verified; clean teardown leaves zero dangling hooks and zero leaked handles.

---

### 2.3 Subphase 2C.2: Context Snapshot & AgentExecutor Integration
- **Objective**: Format active desktop context into defensive, bounded prompt blocks for `AgentExecutor`.
- **Scope**:
  - Implement `ContextSnapshotBuilder` with item-level sensitivity filtering.
  - Implement defensive delimiter wrapping (`<environmental_context untrusted="true">`).
  - Implement natural language prompt-injection defense guidelines.
  - Implement Model-Provider Egress Policy: `PUBLIC_CONTEXT` and sanitized `PERSONAL_CONTEXT` permitted; `PROTECTED_CONTEXT` blocked.
  - Implement MCP Isolation Policy: Ambient context stripped from outgoing tool parameters.
  - Enforce context precedence hierarchy in `ContextAssembler.assemble()`.
  - Enforce subagent context isolation (empty ambient context by default).
- **Files Created**:
  - `runtime/context/snapshot.py`
  - `runtime/context/adapter.py`
  - `tests/test_agent_context_integration.py`
  - `tests/adversarial/test_adv_context_injection.py`
- **Files Modified**:
  - `runtime/agent_executor.py` (Pass validated snapshot to assembler)
  - `runtime/reconstruction.py` (Format untrusted environmental context block)
- **Security Gate**: Environmental text classified as untrusted; 20 prompt-injection adversarial test cases pass.
- **Design Resource Budget**: Max 2 KB prompt context payload; zero default disk persistence.
- **Exit Criteria**: Prompt injection attacks fail closed; subagent isolation verified; MCP tools receive clean arguments.

---

### 2.4 Subphase 2C.3: Native Observation Capabilities
- **Objective**: Expose native desktop observation functions as standard tools in `ToolRegistry`.
- **Scope**:
  - Implement `desktop.get_foreground_window` (Class A non-blocking).
  - Implement `desktop.list_open_windows` (Class A with cloaking filter, max 25 windows).
  - Implement `system.get_resources` (Class A Win32 telemetry).
  - Register tools into `ToolRegistry` using exact `AuthorityLevel.L0_OBSERVE` and `ToolDefinition`.
- **Files Created**:
  - `runtime/capabilities/__init__.py`
  - `runtime/capabilities/contracts.py`
  - `runtime/capabilities/dispatcher.py`
  - `runtime/capabilities/windows/__init__.py`
  - `runtime/capabilities/windows/observation.py`
  - `tests/test_capabilities_observation.py`
- **Files Modified**:
  - `runtime/tools/registry.py` (Register native observation tool definitions)
- **Security Gate**: Observation tools strictly read-only; zero system state mutation.
- **Design Resource Budget**: Execution latency < 20 ms per observation call; memory delta < 0.1 MB.
- **Exit Criteria**: Capabilities pass schema validation; output correctly captured in trajectory step.

---

### 2.5 Subphase 2C.4: First Reversible Windows Execution Capabilities
- **Objective**: Implement controlled, safe desktop execution capabilities under `AuthorityLevel.L2_EXECUTE_REVERSIBLE`.
- **Scope**:
  - Implement `desktop.activate_window` (Class A best-effort activation with `WindowIdentity` TOCTOU verification using `expected_creation_filetime_ticks`; `FlashWindowEx` fallback on OS foreground lock).
  - Implement `shell.open_path` as a **Class C (Best-Effort Asynchronous Shell Request)**:
    * Win32 API: `ShellExecuteExW` with `fMask = SEE_MASK_FLAG_NO_UI (0x0400) | SEE_MASK_ASYNCOK (0x00100000) | SEE_MASK_NOCLOSEPROCESS (0x00000040)`.
    * `SEE_MASK_NOZONECHECKS (0x00000008)` is **PROHIBITED**; Windows security zones and Attachment Execution Service policies are strictly preserved.
    * Handle caller ownership: If `sei.hProcess` is non-NULL, caller owns the handle and closes it immediately via `CloseHandle()`.
    * Disambiguate shell association from Job Object process containment: Shell-executed applications run in the user's interactive desktop session outside Presence's Job hierarchy.
    * Python wrapper timeout (`anyio.fail_after(5.0)`): Cancels awaiting coroutine; acknowledges underlying worker thread cannot be forcibly aborted if blocked in shell/COM handler.
    * Executable blacklisting: Direct opening of executable files (`.exe`, `.bat`, `.cmd`, `.ps1`, `.vbs`, `.msi`) is rejected.
  - Clipboard reading (`presence.read_clipboard`) is **DEFERRED**.
- **Files Created**:
  - `runtime/capabilities/windows/execution.py`
  - `tests/test_capabilities_execution.py`
  - `tests/adversarial/test_adv_capabilities_permissions.py`
- **Files Modified**:
  - `runtime/permissions.py` (Declare capability policies for L2 tools)
  - `runtime/tools/registry.py` (Register L2 execution tool definitions)
- **Security Gate**: Executable launching blocked in `shell.open_path`; `SetForegroundWindow` respects OS lock; zero `AttachThreadInput` hacks; zone checks preserved.
- **Resource Budget**: Max execution timeout 2.0s (activate) / 5.0s (shell wrapper); worker thread execution prevents event loop blocking.
- **Exit Criteria**: Window activation verified; recycled HWND fails closed via FILETIME ticks; zone checks preserved; no kernel handle leaks on returned `hProcess`.

---

### 2.6 Subphase 2C.5: Application-Specific Context Provider Foundation
- **Objective**: Add deep context extraction for developer applications (VS Code and Windows Terminal) via authenticated local IPC.
- **Scope**:
  - **VS Code Integration (Option C — P1-01, P2C-19)**:
    * 4-Layer Authenticated Named Pipe Architecture:
      - **Layer 1 (Transport Access Control)**: Pipe name `\\.\pipe\presence-vscode-<user>-<session>`. Created with `PIPE_REJECT_REMOTE_CLIENTS` (0x00000008) and `FILE_FLAG_FIRST_PIPE_INSTANCE` (0x00080000). Explicit DACL granting `GENERIC_READ | GENERIC_WRITE` only to the user's Logon SID and User SID. Rejects remote network clients and other desktop sessions.
      - **Layer 2 (Peer Process Identity Verification)**: Server calls `GetNamedPipeClientProcessId()`, validates client executable path is authentic `Code.exe` via `QueryFullProcessImageNameW()`, checks process creation ticks, and inspects client token to verify identical Logon SID.
      - **Layer 3 (Application Pairing & Mutual Authentication)**: One-time user-initiated pairing secret generated by Presence, passed via command-line activation or user copy-paste, stored in VS Code's `SecretStorage` (DPAPI/Credential Manager). Mutual HMAC-SHA256 challenge-response authentication with 32-byte cryptographic nonces; verified using constant-time comparison (`secrets.compare_digest`). Negotiates ephemeral session token.
      - **Layer 4 (Framing, Payload Bounds & Operation Authorization)**: 4-byte length-prefixed framing with a hard 64 KB payload ceiling. Strict JSON-RPC schema allowlist for read-only metadata: `get_active_workspace`, `get_open_documents`, `get_active_language_id`. Arbitrary file reading, code execution, and telemetry modification are strictly prohibited.
    * Threat model: Documents defense-in-depth boundary against same-user unprivileged malware.
    * Option A (window title heuristic) used strictly as fallback when extension is absent or unauthenticated.
  - **Windows Terminal Integration (Option C — P1-02)**:
    * Acknowledge that Windows Terminal provides no public external query API for cwd.
    * Define opt-in shell integration hook (PowerShell `$PROFILE` / CMD hook) emitting cwd to a local Presence named pipe with identical DACL transport security.
    * Option A (tab title parsing) used strictly as fallback.
  - Provider isolation: All provider IPC wrapped in Class B 500 ms timeouts; provider failure fails gracefully to generic window metadata.
- **Files Created**:
  - `runtime/context/providers/__init__.py`
  - `runtime/context/providers/vscode.py`
  - `runtime/context/providers/terminal.py`
  - `runtime/context/providers/ipc_security.py`
  - `tests/test_context_providers.py`
  - `tests/adversarial/test_adv_ipc_authentication.py`
- **Files Modified**:
  - `runtime/context/manager.py` (Query active application provider)
- **Security Gate**: Named pipe authentication verified across all 4 layers; client identity validated; replay attacks rejected; provider timeouts prevent thread blocking.
- **Resource Budget**: Provider execution capped at 500 ms; provider crashes isolated without killing ContextManager.
- **Exit Criteria**: VS Code workspace path accurately resolved; terminal cwd extracted; graceful fallback on crash; unauthenticated pipe clients rejected.

---

## 3. Package Structure Proposal & Component Ownership

```
runtime/
├── context/
│   ├── __init__.py                # ContextManager, ContextSnapshot public exports
│   ├── models.py                  # Deeply immutable value contracts (ContextSnapshot, WindowIdentity)
│   ├── contracts.py               # Abstract interfaces (ContextSource, ContextAdapter, Provider)
│   ├── source.py                  # Base ContextSource class and lifecycle state machine
│   ├── adapter.py                 # Normalization, sanitization, item-level classification
│   ├── manager.py                 # ContextManager lifecycle, debouncing, snapshot cache
│   ├── snapshot.py                # Point-in-time snapshot builder and bounding
│   ├── subscriptions.py           # Topic-based filtered subscriber router
│   ├── expiration.py              # Expiration, TTL, and cache-eviction policies
│   ├── sources/
│   │   ├── __init__.py
│   │   └── foreground_window.py   # WinEventHookThread and User32 window observer
│   └── providers/
│       ├── __init__.py
│       ├── ipc_security.py        # Named pipe DACL, peer verification, HMAC challenge-response
│       ├── vscode.py              # Visual Studio Code authenticated provider
│       └── terminal.py            # Windows Terminal shell integration provider
│
└── capabilities/
    ├── __init__.py                # Capability registration exports
    ├── contracts.py               # CapabilityContract, ExecutionResult models
    ├── dispatcher.py              # Thread-safe capability dispatcher
    └── windows/
        ├── __init__.py
        ├── observation.py         # desktop.get_foreground_window, desktop.list_open_windows
        └── execution.py           # desktop.activate_window, shell.open_path
```

---

## 4. Resource Measurement Methodology & Acceptance Budgets (P1-03, P2C-20)

To ensure scientific rigor, Phase 2C performance is evaluated using standardized measurement scripts (`tests/diagnose_resource_telemetry.py`) executing six discrete test experiments:

### 4.1 Standardized Experiments (A through F)

1. **Experiment A — Cold Baseline**: Record idle working set, private bytes, thread count, and kernel handle count of the Python process prior to initializing `ContextManager`.
2. **Experiment B — Hook Initialization**: Start `ForegroundWindowSource`. Assert that exactly one OS thread (`WinEventHookThread`) is added via Toolhelp32 snapshot. Record handle allocation delta.
3. **Experiment C — Steady-State Idle (300s Soak)**: Monitor the runtime for 300 seconds without user window switches. Verify that the hook thread sleeps inside `GetMessageW` (kernel wait). Record CPU time, wakeups, and working set drift.
4. **Experiment D — Foreground Switch Burst**: Programmatically trigger 50 rapid foreground window focus changes across 2 seconds. Verify that the reentrancy guard (`_in_callback`) prevents recursion, the bounded queue (`deque(maxlen=50)`) absorbs events without growth, and 150 ms debouncing coalesces duplicate events.
5. **Experiment E — Sustained Interaction (10-Minute Soak)**: Generate 500 window transition events over 10 minutes. Monitor working set and handle counts. Verify garbage collection stability and ensure working set plateaus without linear growth.
6. **Experiment F — Teardown & Cleanliness**: Stop `ContextManager`. Assert that `UnhookWinEvent` is called, `WM_QUIT` terminates `GetMessageW`, the hook thread joins within 1.0 second, and net handle and thread counts return exactly to cold baseline.

### 4.2 Resource Budgets: Design Target vs Acceptance Ceiling

Phase 2C distinguishes between optimal **Design Targets** and binding **Acceptance Ceilings**:

| Resource Metric | Measurement Condition | Design Target | Acceptance Ceiling | Enforcement Action |
|---|---|---|---|---|
| **Idle CPU Utilization** | Steady-state idle (Exp C) | < 0.05% | < 0.20% | Profiling audit if ceiling breached |
| **Hook Worker Threads** | Active runtime (Exp B, C) | Exactly 1 thread | Exactly 1 thread | Hard test failure if > 1 |
| **Working Set Overhead** | Active runtime vs baseline | < 3.5 MB | < 8.0 MB | Memory leak audit if ceiling breached |
| **Burst CPU Utilization** | 50 events / 2s burst (Exp D) | < 1.0% | < 3.0% | Debounce tuning if ceiling breached |
| **Net Kernel Handle Leaks** | Post-teardown vs baseline (Exp F) | Exactly 0 | Exactly 0 | Hard test failure if > 0 |
| **Capability Dispatch Latency**| Class A observation calls | < 15 ms | < 50 ms | Query optimization if ceiling breached |

---

## 5. Explicit Deferred Work & Out-of-Scope Boundaries

The following items are **explicitly out-of-scope for Phase 2C**:
1. **No UI / Frontend / Floating Bar / Dynamic Island**: Presentation belongs in Phase 3.
2. **No Screen Recording / Video Streaming / Continuous OCR**: Explicitly prohibited.
3. **No Keylogging or Global Input Hooking**: Keystrokes are never captured.
4. **No Clipboard Reading**: Deferred to a future milestone with trusted UI consent.
5. **No Synthetic Input Injection (`SendInput`)**: Brittle mouse/keyboard automation rejected.
6. **No Arbitrary Shell Code Execution**: All actions flow through structured capability handlers.
7. **No Rust Sidecars or New Binary Compilers**: Pure Python 3.12 with standard `ctypes`.
