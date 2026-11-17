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
  - Dedicate `WinEventHookThread` with Win32 message pump (`GetMessageW` / `TranslateMessage` / `DispatchMessageW`).
  - Implement hook callback reentrancy guard (`_in_callback` flag) and bounded event queuing (`collections.deque(maxlen=64)`).
  - Implement **Bounded Event Dispatch & Unified Notification Synchronization (P2C-24, P2C-29, P2C-33, P2C-35, P2C-37, P2C-39, P2C-40, P2C-41, P2C-42, P2C-43, P2C-44, P2C-46, P2C-47, P2C-48, P2C-49, P2C-50, P2C-51, P2C-52, P2C-53)**:
    * Single monitor on `BoundedEventQueue`: synchronizes both the ring deque and notification state, completely eliminating secondary blocking locks (`_state_lock` eliminated).
    * Strictly non-blocking callback: `push()` acquires `_lock.acquire(blocking=False)`. If contended, immediately drops event, increments `events_dropped_contention`, and returns `(False, False)` without blocking the Win32 message pump.
    * Monitor ownership rules (P2C-51): lock acquisition strictly restricted to `push()` and `drain_and_reset()`. Diagnostic readers (`notification_pending`, `len()`) read attributes directly without acquiring `_lock`, guaranteeing zero reader-induced contention drops.
    * Exact usable capacity: strictly **64 entries**. Overflow drops incoming event and increments `events_dropped_capacity`.
    * Single-pending notification: `push()` sets `_notification_pending = True` only on False->True transition and returns `schedule_signal = True`; otherwise increments `events_coalesced`.
    * Zero stranded events: `drain_and_reset()` atomically resets `_notification_pending = False` and batch-drains the deque ($O(n)$ bounded by $O(\text{capacity} = 64)$).
    * CPython 3.12.7 Signalling Semantics (P2C-43, P2C-48): `loop.call_soon_threadsafe(self._event_available.set)` appends to `_ready` directly and executes non-blocking `_write_to_self()` (loopback socket send). Thread-safe scheduling without internal locks; no hard real-time guarantee.
    * Failure handling atomicity (P2C-49): If scheduling fails, queue transitions to `_faulted = True`, retaining `_notification_pending = True` to preserve the invariant:
      $$\_notification\_pending == \text{False} \implies \text{queue\_depth} == 0$$
    * CPython GIL & latency reality: Zero application-level blocking lock waits. GIL acquisition latency is governed by `sys.getswitchinterval()` (ideal evaluation timeslice, not a hard real-time ceiling). No unmeasured `<50 μs` or `0.2–0.5 μs` claims.
    * Memory footprint precision (P2C-53): Queue data structures heap footprint verified at exactly $9,216 + 1,288 + 568 + 56 = \mathbf{11,128 \text{ bytes}}$ ($10.87 \ \text{KB}$ in Python 3.12 AMD64), strictly distinguished from total process working set ceiling ($< 8.0 \ \text{MB}$ above Phase 2B baseline).
    * Composite WindowIdentity Freshness Contract (P2C-47, P2C-52): Consumer extracts initial composite identity (`HWND`, `PID`, `creation_filetime_ticks`, `window_class`), extracts metadata, then performs post-collection re-validation against all 4 fields. Mid-inspection focus shifts and HWND recycling attacks are cleanly aborted (`stale_snapshots_aborted += 1`).
    * Supervised Single-Consumer Worker: runs a dedicated, persistent worker task with invariant `active_foreground_consumer_count <= 1`.
    * Strict Lifecycle Ordering (P2C-50): Hook unhooks on owning thread and terminates BEFORE consumer worker stops and loop is stopped/closed.
    * Guaranteed invariants:
      $$\text{pending\_notification\_count} \le 1, \quad \text{active\_foreground\_consumer\_count} \le 1, \quad \text{queue\_depth} \le 64 \quad \forall t$$
  - Instrumentation counters: `raw_events_received`, `events_coalesced`, `events_dropped_capacity`, `events_dropped_contention`, `queue_high_water_mark`, `notifications_scheduled`, `notifications_executed`, `scheduling_failures`, `max_event_processing_delay`, `pending_notification_count`, `active_foreground_consumer_count`, `authoritative_refreshes`, `stale_snapshots_aborted`, `snapshots_committed`.
  - Retrieve top-level window caption via `GetWindowTextW` (User32 cached caption; no synthetic timeouts).
  - Construct composite `WindowIdentity` (`HWND`, `PID`, `process_creation_filetime_ticks: int`, `window_class`).
  - Implement 150 ms event debouncing and duplicate suppression.
  - Implement **Owner-Controlled Shutdown (P2C-25)**:
    * Hook unhooking (`UnhookWinEvent`) is executed strictly by `WinEventHookThread` upon receiving `WM_QUIT`.
    * Runtime thread joins hook thread with 1.0s timeout.
    * If `thread.is_alive()` after timeout, report `HookShutdownTimeoutError` (do not claim clean termination).
- **Files Created**:
  - `runtime/context/sources/__init__.py`
  - `runtime/context/sources/foreground_window.py`
  - `tests/test_win32_foreground_source.py`
  - `tests/adversarial/test_adv_context_debouncing.py`
  - `tests/adversarial/test_adv_hook_reentrancy.py`
  - `tests/adversarial/test_adv_hook_backpressure.py`
  - `tests/adversarial/test_adv_hook_lock_contention.py`
  - `tests/adversarial/test_adv_hook_consumer_delay.py`
  - `tests/adversarial/test_adv_hook_scheduling_failure.py`
  - `tests/adversarial/test_adv_hook_drain_race.py`
  - `tests/adversarial/test_adv_hook_reset_race.py`
  - `tests/adversarial/test_adv_hook_queue_overflow.py`
  - `tests/adversarial/test_adv_hook_shutdown_pending.py`
  - `tests/adversarial/test_adv_hook_repeated_lifecycle.py`
  - `tests/adversarial/test_adv_hook_producer_during_drain.py`
  - `tests/adversarial/test_adv_hook_producer_after_reset.py`
  - `tests/adversarial/test_adv_hook_concurrent_producer_consumer.py`
  - `tests/adversarial/test_adv_hook_events_during_async_work.py`
  - `tests/adversarial/test_adv_hook_event_loop_closed.py`
  - `tests/adversarial/test_adv_hook_event_loop_stalled.py`
  - `tests/adversarial/test_adv_hook_single_consumer_invariant.py`
  - `tests/adversarial/test_adv_hook_bounded_task_count.py`
  - `tests/adversarial/test_adv_hook_late_callback_after_shutdown.py`
- **Files Modified**:
  - `runtime/platform_win32.py` (Add User32/Kernel32 window inspection bindings)
  - `runtime/context/manager.py` (Register `ForegroundWindowSource`)
- **Security Gate**: Out-of-context hook only (no DLL injection); handle isolation verified via `CloseHandle`.
- **Resource Budget**:
  - Thread Count: Exactly 1 background OS thread (`WinEventHookThread`).
  - Idle CPU: Design Target < 0.05%; Acceptance Ceiling < 0.20% (event-driven kernel wait in `GetMessageW`).
  - Working Set Overhead: Design Target < 3.5 MB; Acceptance Ceiling < 8.0 MB.
- **Exit Criteria**: Hook reliably detects window switches; callback reentrancy verified; debouncing verified; single-pending-notification verified under burst; clean teardown leaves zero dangling hooks and zero leaked handles.

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
  - **VS Code Integration (Option C — P1-01, P2C-19, P2C-21, P2C-22, P2C-26, P2C-27)**:
    * 4-Layer Authenticated Named Pipe Architecture:
      - **Layer 1 (Transport Access Control, DACL Hardening & Owner Rights)**:
        * Pipe name `\\.\pipe\presence-vscode-<username>-<logon_session_id>`.
        * Parameter placement: `dwOpenMode` (`PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE | FILE_FLAG_OVERLAPPED` = `0x40080003`); `dwPipeMode` (`PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_REJECT_REMOTE_CLIENTS | PIPE_WAIT` = `0x00000008`).
        * Hardened DACL with Owner Rights restriction (P2C-28): `D:P(A;;RC;;;OW)(A;;0x12019b;;;<LogonSID>)`.
        * `OW` represents the object owner (`TokenOwner`, which is the user account SID). The Logon SID cannot be set as object owner (`ERROR_INVALID_OWNER`, 1307). Setting `(A;;RC;;;OW)` explicitly restricts the owner to `READ_CONTROL` (`0x20000`), stripping default implicit `WRITE_DAC` (`0x40000`), withholding `FILE_CREATE_PIPE_INSTANCE` (`0x0004`), and blocking same-user cross-session IPC access.
        * Data read/write granted strictly via client ACE: `0x0012019B` to active interactive `LogonSID`.
        * `FILE_CREATE_PIPE_INSTANCE` (`0x0004`) is strictly withheld from both client and owner ACEs.
        * `bInheritHandle = FALSE` in `SECURITY_ATTRIBUTES`.
        * Fails closed with `ERROR_ACCESS_DENIED` if pipe squatting is detected.
      - **Layer 2 (Bidirectional Peer Process Identity Verification)**:
        * Server queries `GetNamedPipeClientProcessId()`, executable image path via `QueryFullProcessImageNameW()`, creation ticks via `GetProcessTimes(&client_creation_filetime_ticks)`, and client token Logon SID.
        * Client queries `GetNamedPipeServerProcessId()`, server binary path, creation ticks via `GetProcessTimes(&server_creation_filetime_ticks)`, and server token Logon SID.
      - **Layer 3 (Pairing & Canonical Binary Transcript HKDF-SHA256 Mutual Authentication)**:
        * Short-lived user pairing command: `presence vscode-pair` (256-bit secret $K_{\text{pair}}$, 5-minute TTL, single-use).
        * Stored in VS Code `SecretStorage` (DPAPI) and Presence user vault. Never passed in CLI args, env vars, history, or logs.
        * Canonical binary transcript serialization ($T$) with strict field validation using big-endian integers and length-prefixed strings (strict fullmatch regex `\A...\Z` for endpoints and Logon SIDs, no bare asserts):
          `[uint32_be version][uint16_be ep_len + ep][uint32_be s_pid][uint64_be s_ticks][uint16_be s_sid_len + s_sid][uint32_be c_pid][uint64_be c_ticks][uint16_be c_sid_len + c_sid][32B s_nonce][32B c_nonce][uint32_be max_payload_bytes]`
        * Standard HKDF-SHA256 (RFC 5869) Key Derivation:
          - $\text{PRK} = \text{HKDF-Extract}(\text{salt}=\text{server\_nonce} \parallel \text{client\_nonce}, \text{IKM}=K_{\text{pair}})$.
          - $K_{\text{client\_auth}} = \text{HKDF-Expand}(\text{PRK}, \text{b"PRESENCE_VSCODE_CLIENT_AUTH_V1"}, 32)$.
          - $K_{\text{server\_auth}} = \text{HKDF-Expand}(\text{PRK}, \text{b"PRESENCE_VSCODE_SERVER_AUTH_V1"}, 32)$.
          - $K_{\text{session}} = \text{HKDF-Expand}(\text{PRK}, \text{b"PRESENCE_VSCODE_SESSION_KEY_V1"} \parallel \text{SHA256}(T), 32)$.
        * Mutual challenge-response:
          - $\text{client\_proof} = \text{HMAC-SHA256}(K_{\text{client\_auth}}, T)$, verified with `secrets.compare_digest`.
          - $\text{server\_proof} = \text{HMAC-SHA256}(K_{\text{server\_auth}}, T)$, verified with `secrets.compare_digest`.
        * Session Key ($K_{\text{session}}$) Status: Established and bound to transcript hash $\text{SHA256}(T)$, but **explicitly reserved/deferred** for future post-handshake message framing (Phase 2D). For Phase 2C, data frames travel over the local named pipe secured by Logon SID DACL isolation.
      - **Layer 4 (Framing, Payload Bounds & Canonical Operation Allowlist)**:
        * 4-byte big-endian length prefix (`>I`), hard 64 KB (`65536` bytes) payload ceiling.
        * Canonical operation allowlist: `vscode.ping`, `vscode.get_active_workspace`, `vscode.get_active_editor_metadata`, `vscode.get_open_documents`.
        * `provider.shutdown` is **strictly prohibited**. Read-only metadata only (zero file contents, zero code execution).
    * Threat model: Honestly articulates same-user medium-integrity memory access boundaries.
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
- **Security Gate**: Named pipe authentication verified across all 4 layers; client identity validated; transcript replayed attacks rejected; provider timeouts prevent thread blocking.
- **Resource Budget**: Provider execution capped at 500 ms; provider crashes isolated without killing ContextManager.
- **Exit Criteria**: VS Code workspace path accurately resolved; terminal cwd extracted; graceful fallback on crash; unauthenticated or cross-session pipe clients rejected.

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

## 4. Resource Measurement Methodology & Acceptance Budgets (P1-03, P2C-20, P2C-24)

To ensure scientific rigor, Phase 2C performance is evaluated using standardized measurement scripts (`tests/diagnose_resource_telemetry.py`) executing seven discrete test experiments:

### 4.1 Standardized Experiments (A through G)

1. **Experiment A — Cold Baseline**: Record idle working set, private bytes, thread count, and kernel handle count of the Python process prior to initializing `ContextManager`.
2. **Experiment B — Hook Initialization**: Start `ForegroundWindowSource`. Assert that exactly one OS thread (`WinEventHookThread`) is added via Toolhelp32 snapshot. Record handle allocation delta.
3. **Experiment C — Steady-State Idle (300s Soak)**: Monitor the runtime for 300 seconds without user window switches. Verify that the hook thread sleeps inside `GetMessageW` (kernel wait). Record CPU time, wakeups, and working set drift.
4. **Experiment D — Foreground Switch Burst**: Programmatically trigger 50 rapid foreground window focus changes across 2 seconds. Verify that the reentrancy guard (`_in_callback`) prevents recursion, the bounded queue (`deque(maxlen=50)`) absorbs events without growth, and 150 ms debouncing coalesces duplicate events.
5. **Experiment E — Sustained Interaction (10-Minute Soak)**: Generate 500 window transition events over 10 minutes. Monitor working set and handle counts. Verify garbage collection stability and ensure working set plateaus without linear growth.
6. **Experiment F — Teardown & Cleanliness**: Stop `ContextManager`. Assert that `UnhookWinEvent` is called strictly by `WinEventHookThread`, `WM_QUIT` terminates `GetMessageW`, the hook thread joins within 1.0 second, and net handle and thread counts return exactly to cold baseline. If join times out, report failure state.
7. **Experiment G — Stalled Consumer Backpressure Stress Test (P2C-24)**: Artificially delay the asyncio consumer by 1.0 second during a burst of 500 synthetic window events. Assert that `_raw_event_queue` length does not exceed 50, `pending_notification_count <= 1` is strictly maintained, `events_dropped` and `events_coalesced` account for all suppressed events, and the asyncio queue is not flooded.

### 4.2 Resource Budgets: Design Target, Acceptance Ceiling & Evidence Classification

Phase 2C rigorously distinguishes between **Design Targets**, binding **Acceptance Ceilings**, and actual **Measured Results**:

| Resource Metric | Measurement Condition | Design Target | Acceptance Ceiling | Evidence Classification | Enforcement Action |
|---|---|---|---|---|---|
| **Idle CPU Utilization** | Steady-state idle (Exp C) | < 0.05% | < 0.20% | DESIGN ONLY | Profiling audit if ceiling breached |
| **Hook Worker Threads** | Active runtime (Exp B, C) | Exactly 1 thread | Exactly 1 thread | DESIGN ONLY | Hard test failure if > 1 |
| **Working Set Overhead** | Active runtime vs baseline | < 3.5 MB | < 8.0 MB | DESIGN ONLY | Memory leak audit if ceiling breached |
| **Burst CPU Utilization** | 50 events / 2s burst (Exp D) | < 1.0% | < 3.0% | DESIGN ONLY | Debounce tuning if ceiling breached |
| **Pending Notification Count** | Any runtime state (Exp D, G) | ≤ 1 callback | Exactly 1 callback | DESIGN ONLY | Hard test failure if > 1 |
| **Raw Event Queue Size** | Any runtime state (Exp D, G) | ≤ 50 items | Exactly 50 items | DESIGN ONLY | Hard test failure if > 50 |
| **Net Kernel Handle Leaks** | Post-teardown vs baseline (Exp F) | Exactly 0 | Exactly 0 | DESIGN ONLY | Hard test failure if > 0 |
| **Capability Dispatch Latency**| Class A observation calls | < 15 ms | < 50 ms | DESIGN ONLY | Query optimization if ceiling breached |

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
