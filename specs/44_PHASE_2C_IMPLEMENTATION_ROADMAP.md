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
- **Design Resource Budget**: Zero OS threads added; < 1 MB memory overhead.
- **Exit Criteria**: 100% unit test pass on context models, state transitions, TTL expiration, and EventBus dispatches.

---

### 2.2 Subphase 2C.1: Native Foreground-Window Awareness
- **Objective**: Connect `ContextManager` to real Windows desktop events via native Win32 APIs.
- **Scope**:
  - Implement `ForegroundWindowSource` using `SetWinEventHook(EVENT_SYSTEM_FOREGROUND)`.
  - Dedicate `WinEventHookThread` with Win32 message pump (`GetMessageW` / `DispatchMessageW`).
  - Retrieve top-level window caption via `GetWindowTextW` (User32 cached caption; no synthetic timeouts).
  - Construct composite `WindowIdentity` (`HWND`, `PID`, `process_creation_time_ns`, `window_class`).
  - Implement 150 ms event debouncing and duplicate suppression.
  - Guarantee clean unhooking via `UnhookWinEvent`, `WM_QUIT`, and thread join.
- **Files Created**:
  - `runtime/context/sources/__init__.py`
  - `runtime/context/sources/foreground_window.py`
  - `tests/test_win32_foreground_source.py`
  - `tests/adversarial/test_adv_context_debouncing.py`
- **Files Modified**:
  - `runtime/platform_win32.py` (Add User32/Kernel32 window inspection bindings)
  - `runtime/context/manager.py` (Register `ForegroundWindowSource`)
- **Security Gate**: Out-of-context hook only (no DLL injection); handle isolation verified via `CloseHandle`.
- **Design Resource Budget**: Exactly 1 background OS thread added; < 3.5 MB working set overhead; 0.0% idle CPU.
- **Exit Criteria**: Hook reliably detects window switches; debouncing verified; clean teardown leaves zero dangling hooks.

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
  - Implement `desktop.activate_window` (Class A best-effort activation with `WindowIdentity` TOCTOU verification; `FlashWindowEx` fallback on OS foreground lock).
  - Implement `shell.open_path` (Class B path opening with `ShellExecuteExW`, executable blacklist, and preserved security zone checks; **no `SEE_MASK_NOZONECHECKS`**).
  - Disambiguate shell association from Job Object process containment.
  - Clipboard reading (`presence.read_clipboard`) is **DEFERRED**.
- **Files Created**:
  - `runtime/capabilities/windows/execution.py`
  - `tests/test_capabilities_execution.py`
  - `tests/adversarial/test_adv_capabilities_permissions.py`
- **Files Modified**:
  - `runtime/permissions.py` (Declare capability policies for L2 tools)
  - `runtime/tools/registry.py` (Register L2 execution tool definitions)
- **Security Gate**: Executable launching blocked in `shell.open_path`; `SetForegroundWindow` respects OS lock; zero `AttachThreadInput` hacks.
- **Design Resource Budget**: Max execution timeout 2.0s; worker thread execution prevents event loop blocking.
- **Exit Criteria**: Window activation verified; recycled HWND fails closed; zone checks preserved.

---

### 2.6 Subphase 2C.5: Application-Specific Context Provider Foundation
- **Objective**: Add deep context extraction for developer applications (VS Code and Windows Terminal).
- **Scope**:
  - **VS Code Integration (Option C — P1-01)**:
    * Define lightweight extension contract communicating over authenticated local named pipe (`\\.\pipe\presence-vscode-<user>`).
    * Exposes read-only metadata: workspace path, open folder names, active language ID.
    * No arbitrary file content or keystroke access.
    * Option A (window title heuristic) used strictly as fallback when extension is absent.
  - **Windows Terminal Integration (Option C — P1-02)**:
    * Acknowledge that Windows Terminal provides no public external query API for cwd.
    * Define opt-in shell integration hook (PowerShell `$PROFILE` / CMD hook) emitting cwd to a local Presence named pipe.
    * Option A (tab title parsing) used strictly as fallback.
  - Provider isolation: All provider IPC wrapped in Class B 500 ms timeouts; provider failure fails gracefully to generic window metadata.
- **Files Created**:
  - `runtime/context/providers/__init__.py`
  - `runtime/context/providers/vscode.py`
  - `runtime/context/providers/terminal.py`
  - `tests/test_context_providers.py`
- **Files Modified**:
  - `runtime/context/manager.py` (Query active application provider)
- **Security Gate**: Named pipe authentication verified; provider timeouts prevent thread blocking.
- **Design Resource Budget**: Provider execution capped at 500 ms; provider crashes isolated without killing ContextManager.
- **Exit Criteria**: VS Code workspace path accurately resolved; terminal cwd extracted; graceful fallback on crash.

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
│       ├── vscode.py              # Visual Studio Code workspace provider
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

## 4. Resource Measurement Methodology (P1-03)

To ensure scientific rigor, Phase 2C performance will be verified using standardized measurement scripts (`tests/diagnose_resource_telemetry.py`):
1. **Baselines**: Record idle working set and private bytes before `ContextManager.start()`.
2. **Hook Thread Verification**: Verify exactly 1 additional thread via Toolhelp32 snapshot.
3. **Event Burst Stress**: Trigger 50 rapid foreground window changes in 2 seconds; assert memory delta < 1.0 MB and queue size remains bounded.
4. **Steady-State Idle**: Monitor process over 60 seconds with zero window changes; assert 0.0% CPU usage and zero wakeups.
5. **Teardown Plateau**: Verify handle and thread counts return to baseline after `ContextManager.stop()`.

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
