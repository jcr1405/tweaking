# SPECIFICATION 44 — PHASE 2C: IMPLEMENTATION ROADMAP
# Persistent Intelligent Presence — Subphase Breakdown, Dependencies, Testing & Exit Criteria

## 1. Roadmap Architecture & Phasing Strategy

Phase 2C connects the Presence intelligence backend to the active Windows desktop environment. To maintain engineering rigor, prevent regressions, and enforce resource discipline, Phase 2C is partitioned into **six discrete, testable subphases (2C.0 through 2C.5)**.

Each subphase has its own explicit scope, dependency boundaries, security gates, resource budgets, and exit criteria. No subphase begins until the prior subphase passes its verification suite.

```
+---------------------------------------------------------------------------------------------------+
|                               PHASE 2C IMPLEMENTATION SEQUENCE                                    |
+---------------------------------------------------------------------------------------------------+
  Phase 2C.0: Context Contracts & Lifecycle Foundation
      │       (Models, interfaces, state machine, EventBus integration, expiration)
      ▼
  Phase 2C.1: Native Foreground-Window Context
      │       (WinEventHookThread, User32 bindings, debouncing, process identity resolution)
      ▼
  Phase 2C.2: Context Snapshot & AgentExecutor Integration
      │       (Snapshot builder, prompt block formatting, untrusted delimiter injection)
      ▼
  Phase 2C.3: Native Observation Capabilities
      │       (ToolRegistry integration, desktop.get_foreground_window, desktop.list_open_windows)
      ▼
  Phase 2C.4: First Reversible Windows Execution Capabilities
      │       (desktop.activate_window, shell.open_path with Job containment, L2/L3 permissions)
      ▼
  Phase 2C.5: Application-Specific Context Provider Foundation
              (VS Code & Windows Terminal workspace adapters, IPC protocol, validation)
```

---

## 2. Detailed Subphase Specifications

### 2.1 Subphase 2C.0: Context Contracts & Lifecycle Foundation
- **Objective**: Establish core context data contracts, interfaces, and state machines without native OS bindings.
- **Scope**:
  - `ContextObservation`, `ContextSnapshot`, `ContextProvenance`, `ContextSensitivity` models.
  - `ContextSource` abstract base class and lifecycle state machine (`UNINITIALIZED` -> `RUNNING` -> `STOPPED`).
  - `ContextManager` coordinator with mock context source.
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
  - `runtime/events.py` (Add typed context event classes)
- **Security Gate**: All dataclasses frozen/immutable; sensitivity enum enforced on all observation models.
- **Resource Budget**: Zero OS threads added; < 1 MB memory overhead.
- **Exit Criteria**: 100% unit test pass on context models, state transitions, TTL expiration, and EventBus dispatches.

---

### 2.2 Subphase 2C.1: Native Foreground-Window Context
- **Objective**: Connect `ContextManager` to real Windows desktop events via native Win32 APIs.
- **Scope**:
  - Implement `ForegroundWindowSource` using `SetWinEventHook(EVENT_SYSTEM_FOREGROUND)`.
  - Dedicate `WinEventHookThread` with Win32 message pump (`GetMessageW` / `DispatchMessageW`).
  - Resolve PID via `GetWindowThreadProcessId` and executable path via `QueryFullProcessImageNameW`.
  - Implement 150 ms window text query timeout (`SendMessageTimeoutW`) to prevent hangs on unresponsive windows.
  - Implement 150 ms event debouncing and duplicate suppression.
- **Files Created**:
  - `runtime/context/sources/__init__.py`
  - `runtime/context/sources/foreground_window.py`
  - `tests/test_win32_foreground_source.py`
  - `tests/adversarial/test_adv_context_debouncing.py`
- **Files Modified**:
  - `runtime/platform_win32.py` (Add User32 window inspection bindings)
  - `runtime/context/manager.py` (Register `ForegroundWindowSource`)
- **Security Gate**: Out-of-context hook only (no DLL injection); handle isolation verified via `CloseHandle`.
- **Resource Budget**: Exactly 1 background OS thread added; < 3 MB working set overhead; 0.0% idle CPU.
- **Exit Criteria**: Hook cleanly detects real window switches; debouncing verified; `UnhookWinEvent` verified on stop.

---

### 2.3 Subphase 2C.2: Context Snapshot & AgentExecutor Integration
- **Objective**: Format active desktop context into defensive, bounded prompt blocks for `AgentExecutor`.
- **Scope**:
  - Implement `ContextSnapshotBuilder` with length clamping (max 200 chars title, max 50 chars app).
  - Implement defensive delimiter wrapping (`<environmental_context untrusted="true">`).
  - Integrate snapshot acquisition into `AgentExecutor.execute_turn()` before model generation.
  - Enforce context precedence: System Guardrails > User Message > Selected Context > Ambient Context > History.
  - Implement subagent context isolation (subagents do not inherit ambient context by default).
- **Files Created**:
  - `runtime/context/snapshot.py`
  - `runtime/context/adapter.py`
  - `tests/test_agent_context_integration.py`
  - `tests/adversarial/test_adv_context_injection.py`
- **Files Modified**:
  - `runtime/agent_executor.py` (Inject snapshot into turn prompt assembly)
- **Security Gate**: Sanitizer neutralizes XML tags and instruction-spoofing markers in window titles.
- **Resource Budget**: Max 2 KB prompt context payload; zero disk persistence for ambient snapshots.
- **Exit Criteria**: 20 prompt-injection adversarial test cases rejected; subagent isolation verified.

---

### 2.4 Subphase 2C.3: Native Observation Capabilities
- **Objective**: Expose native desktop observation functions as standard `L0_OBSERVATION` tools in `ToolRegistry`.
- **Scope**:
  - Implement `desktop.get_foreground_window` (inspect active window metadata).
  - Implement `desktop.list_open_windows` (enumerate visible top-level windows with cloaking filter).
  - Implement `system.get_resources` (CPU, RAM, battery telemetry).
  - Register tools into `ToolRegistry` with strict JSON schemas.
- **Files Created**:
  - `runtime/capabilities/__init__.py`
  - `runtime/capabilities/contracts.py`
  - `runtime/capabilities/dispatcher.py`
  - `runtime/capabilities/windows/__init__.py`
  - `runtime/capabilities/windows/observation.py`
  - `tests/test_capabilities_observation.py`
- **Files Modified**:
  - `runtime/tools/registry.py` (Register native capability tool handlers)
- **Security Gate**: Observation tools strictly read-only; zero system state mutation.
- **Resource Budget**: Execution time < 20 ms per observation call; memory delta < 0.1 MB.
- **Exit Criteria**: Capabilities pass schema validation; output correctly captured in trajectory step.

---

### 2.5 Subphase 2C.4: First Reversible Windows Execution Capabilities
- **Objective**: Implement controlled, safe desktop execution capabilities under `L2_EXECUTION` and `L3_PROTECTED`.
- **Scope**:
  - Implement `desktop.activate_window` (bring window to focus with PID validation).
  - Implement `shell.open_path` (open approved path with executable blacklist).
  - Implement `presence.read_clipboard` (`L3_PROTECTED` with `ConsentToken` HMAC verification).
  - Integrate capability dispatch with `PermissionBoundary`.
- **Files Created**:
  - `runtime/capabilities/windows/execution.py`
  - `runtime/capabilities/windows/clipboard.py`
  - `tests/test_capabilities_execution.py`
  - `tests/adversarial/test_adv_capabilities_permissions.py`
- **Files Modified**:
  - `runtime/permissions.py` (Declare capability permission mappings)
- **Security Gate**: Direct executable launching blocked in `shell.open_path`; clipboard requires L3 consent token.
- **Resource Budget**: Max execution timeout 2.0s; worker thread execution prevents asyncio event loop blocking.
- **Exit Criteria**: Window activation verified; invalid HWND fails closed; unconsented clipboard read blocked.

---

### 2.6 Subphase 2C.5: Application-Specific Context Provider Foundation
- **Objective**: Add deep context extraction for developer applications (VS Code and Windows Terminal).
- **Scope**:
  - Define `ApplicationContextProvider` contract.
  - Implement VS Code workspace detector (extracts active workspace path, Git branch from title/CLI).
  - Implement Windows Terminal detector (extracts current shell and working directory).
  - Provider priority and timeout handling (500 ms provider timeout).
- **Files Created**:
  - `runtime/context/providers/__init__.py`
  - `runtime/context/providers/vscode.py`
  - `runtime/context/providers/terminal.py`
  - `tests/test_context_providers.py`
- **Files Modified**:
  - `runtime/context/manager.py` (Query active application provider)
- **Security Gate**: Provider execution sandboxed; invalid workspace paths rejected.
- **Resource Budget**: Provider execution capped at 500 ms; provider crashes isolated without killing ContextManager.
- **Exit Criteria**: VS Code workspace path accurately resolved; terminal cwd extracted; graceful fallback on crash.

---

## 3. Package Structure Proposal & Component Ownership

```
runtime/
├── context/
│   ├── __init__.py                # ContextManager, ContextSnapshot public exports
│   ├── models.py                  # Immutable dataclasses (Snapshot, Observation, Provenance)
│   ├── contracts.py               # Abstract interfaces (ContextSource, ContextAdapter, Store)
│   ├── source.py                  # Base ContextSource class and lifecycle state machine
│   ├── adapter.py                 # Normalization, sanitization, sensitivity classification
│   ├── manager.py                 # ContextManager lifecycle, debouncing, snapshot cache
│   ├── snapshot.py                # Point-in-time snapshot builder and bounding
│   ├── subscriptions.py           # Topic-based subscription router
│   ├── expiration.py              # Expiration and TTL eviction logic
│   ├── sources/
│   │   ├── __init__.py
│   │   └── foreground_window.py   # WinEventHookThread and User32 window observer
│   └── providers/
│       ├── __init__.py
│       ├── vscode.py              # Visual Studio Code workspace provider
│       └── terminal.py            # Windows Terminal working directory provider
│
└── capabilities/
    ├── __init__.py                # Capability registration exports
    ├── contracts.py               # CapabilityContract, ExecutionResult models
    ├── dispatcher.py              # Thread-safe capability dispatcher
    └── windows/
        ├── __init__.py
        ├── observation.py         # desktop.get_foreground_window, desktop.list_open_windows
        ├── execution.py           # desktop.activate_window, shell.open_path
        └── clipboard.py           # presence.read_clipboard (L3_PROTECTED)
```

### Component Justification:
- `runtime/context/` owns all *ambient observation and normalization* (passive listening).
- `runtime/capabilities/` owns all *active execution and tool bridging* (active doing).
- Existing packages (`runtime/events.py`, `runtime/platform_win32.py`, `runtime/permissions.py`, `runtime/tools/registry.py`) are reused without duplication.

---

## 4. Technical Risks & Failure Recovery Architecture

| Risk ID | Technical Failure Scenario | Impact | Mitigation Architecture |
|---|---|---|---|
| **R-01** | Third-party app hangs during `WM_GETTEXT` | Context thread hangs | Use `SendMessageTimeoutW` with `SMTO_ABORTIFHUNG` and 150 ms limit |
| **R-02** | User holds Alt+Tab creating event flood | EventBus queue overflow | 150 ms debounce timer coalesces events before EventBus publish |
| **R-03** | Window destroyed between query and activate | `SetForegroundWindow` crash | Verify `IsWindow(hwnd)` immediately before activation; fail closed |
| **R-04** | Desktop locked or screensaver active | `GetForegroundWindow` is NULL | Return `None`; pause active context collection until unlock |
| **R-05** | Clipboard locked by another application | `OpenClipboard` returns FALSE | Retry 3 times with 50 ms backoff; return structured error |
| **R-06** | Subagent leaks ambient user context | Cross-agent data leak | Enforce empty environment snapshot by default on subagent creation |
| **R-07** | Hook thread deadlocks during shutdown | Presence hang on exit | Post `WM_QUIT` to hook thread; join with 500 ms hard timeout |

---

## 5. Explicit Deferred Work & Out-of-Scope Boundaries

To maintain engineering discipline and prevent scope creep, the following items are **explicitly out-of-scope for Phase 2C**:
1. **No UI / Frontend / Floating Bar / Dynamic Island**: Phase 2C is strictly backend context and capability integration. UI presentation belongs in Phase 3.
2. **No Screen Recording / Video Streaming / OCR**: Desktop Duplication API and continuous pixel capture are explicitly prohibited.
3. **No Keylogging or Global Input Hooking**: Keystrokes and mouse movement paths are never recorded.
4. **No Rust Sidecars or New Binary Compilers**: The entire subsystem is implemented in Python 3.12 with standard library `ctypes`.
5. **No Direct Shell Script Execution**: Arbitrary `cmd.exe` or `powershell.exe` execution from untrusted model turns is prohibited. All actions flow through structured Win32 capability handlers.
6. **No Modifications to Phase 1 Trajectory Persistence Schemas**: The SQLite database schema is frozen; context metadata uses existing JSON step fields.
