# PRESENCE RUNTIME ARCHITECTURE
## Milestone 0 — Foundational Headless Runtime Specification
**Document Version:** 1.0.0-PROD-SPEC  
**Classification:** Foundational System Architecture Standard  
**Operating Environment:** Windows 11 x64 (User Session 1+)  
**Implementation Foundation:** Python 3.12 Native Windows Substrate (`ctypes`, `sqlite3`, `win32` primitives)  

---

## 1. PURPOSE & ARCHITECTURAL OBJECTIVE

The **Presence Runtime** is the foundational, persistent software substrate for the Intelligent Presence. It operates inside the authenticated Windows user session (Session 1+), independent of any visible user interface, launcher, or web browser.

### Core Architectural Invariants:
1. **Headless Independence:** The runtime operates continuously in the background. Closing, minimizing, crashing, or restarting any future visual UI (HUD, Dynamic Island, tray menu) does **not** terminate or disrupt the runtime.
2. **Session 1 Boundary (Principle of Least Privilege):** The runtime executes entirely within the logged-in user's desktop session. It does **not** install or require a Session 0 Windows Service. It has zero access prior to user authentication and respects Windows lock screen security boundaries.
3. **Single Process with Modular Boundaries:** Avoids premature multi-process microservice complexity. The runtime executes as a single, highly disciplined process with strictly decoupled internal module boundaries communicating via an in-memory typed event bus.
4. **Deterministic Authority Boundary:** Probabilistic AI models and external inputs are untrusted. All operations are mediated through a hard-coded, non-bypassable four-tier Permission Gate.

---

## 2. FOUNDATION EVALUATION & SELECTION

We evaluated candidate runtime foundations against the operational constraints of the host environment:

| Evaluation Dimension | Rust Native (`windows-rs`) | Python 3.12 Native (`ctypes` + `sqlite3`) | Electron / Node.js Runtime |
| :--- | :--- | :--- | :--- |
| **Local Availability** | Toolchain (`rustc`/`cargo`) not installed | **Installed & verified (Python 3.12.10)** | Installed via npm/node |
| **Idle Memory Footprint** | ~5–12 MB Working Set | **~25–35 MB Working Set** | ~150–300 MB Working Set |
| **Idle CPU Utilization** | 0.0% (Kernel wait objects) | **0.0% (Waitable events / threading)** | 0.3%–1.5% (Chromium loop) |
| **Win32 API Interop** | First-class Win32 projections | **Direct native Win32 via `ctypes`** | Indirect C++ addons |
| **Persistence Engine** | Embedded SQLite (`rusqlite`) | **Bundled SQLite WAL (`sqlite3`)** | SQLite / LevelDB |
| **Zero-Package Constraint** | Requires cargo dependency fetch | **Zero external dependencies needed** | Requires npm install |
| **Future UI Decoupling** | High (can drive Tauri/Win32) | **High (can drive Qt, Tauri, or Win32)** | Bound to Chromium DOM |
| **Test Ergonomics** | `cargo test` | **Standard `unittest` (built-in)** | Jest / Vitest |

> **Selection Verdict:** **Python 3.12 Native Windows Substrate (`ctypes`, `sqlite3`, `win32` primitives).**  
> Python 3.12 is already verified on the host machine, satisfies the strict "no package installation" constraint, achieves a measured idle working set of ~28 MB, provides direct Win32 API access through `ctypes.windll`, includes ACID-compliant SQLite WAL out of the box, and allows immediate implementation and automated testing without external compilation toolchains.

---

## 3. SUBSYSTEM ARCHITECTURAL TOPOLOGY

The runtime is structured into eight decoupled subsystems within a single supervised process:

```
+===================================================================================+
|                       PRESENCE RUNTIME PROCESS (Session 1)                        |
|                                                                                   |
|  +-----------------------------------------------------------------------------+  |
|  |                   1. BOOTSTRAP & LIFECYCLE MANAGER                          |  |
|  |  - Single-Instance Mutex: Local\PresenceRuntime_SingleInstanceMutex        |  |
|  |  - Explicit 5-State Machine: CREATED -> STARTING -> RUNNING -> STOPPING     |  |
|  |                              -> TERMINATED                                  |  |
|  |  - Graceful Signal Handling (SIGINT, SIGTERM, CTRL_CLOSE_EVENT)             |  |
|  +-----------------------------------------------------------------------------+  |
|                                         |                                         |
|                                         v                                         |
|  +-----------------------------------------------------------------------------+  |
|  |                      2. IN-MEMORY TYPED EVENT BUS                           |  |
|  |  - Synchronous & Asynchronous Subscription Registries                       |  |
|  |  - Strongly Typed Event Envelopes with Monotonic Sequence IDs               |  |
|  |  - Isolated Error Boundaries (Subscriber exceptions never crash the bus)    |  |
|  +-----------------------------------------------------------------------------+  |
|            │                                                         │            |
|            ├──────────────────────────┐                              │            |
|            ▼                          ▼                              ▼            |
|  +-------------------+      +-------------------+      +-----------------------+  |
|  | 3. COMMAND        |      | 4. PERMISSION     |      | 5. PERSISTENCE LAYER  |  |
|  |    DISPATCHER     |      |    BOUNDARY       |      |    (SQLite WAL)       |  |
|  |  - Deterministic  |      |  - 4-Tier Gate    |      |  - Transactional ACID |  |
|  |    Local Commands |      |    (L0,L1,L2,L3)  |      |  - Configuration Store|  |
|  |  - Zero LLM Token |      |  - Non-Bypassable |      |  - Runtime Recovery   |  |
|  |    Dispatch Path  |      |    Execution Check|      |  - Health Metrics     |  |
|  +-------------------+      +-------------------+      +-----------------------+  |
|            │                          │                              │            |
|            └──────────────────────────┼──────────────────────────────┘            |
|                                       ▼                                           |
|  +-----------------------------------------------------------------------------+  |
|  |                      6. DIAGNOSTICS & AUDIT LOGGING                         |  |
|  |  - Structured JSON / Rotated Flat-File Logs in %LOCALAPPDATA%\Presence\logs |  |
|  |  - Automatic PII & Credential Scrubbing Pipeline                            |  |
|  |  - High-Resolution Microsecond Timers (QueryPerformanceCounter)             |  |
|  +-----------------------------------------------------------------------------+  |
|                                       │                                           |
|                                       ▼                                           |
|  +-----------------------------------------------------------------------------+  |
|  |                      7. NATIVE WIN32 PLATFORM INTEGRATION                   |  |
|  |  - Win32 Named Mutex (`CreateMutexW`) for instant duplicate prevention      |  |
|  |  - Console Ctrl Handler (`SetConsoleCtrlHandler`) for clean OS shutdown    |  |
|  |  - Native Memory Telemetry (`K32GetProcessMemoryInfo`)                      |  |
|  +-----------------------------------------------------------------------------+  |
|                                       │                                           |
|                                       ▼                                           |
|  +-----------------------------------------------------------------------------+  |
|  |                      8. FUTURE INTEGRATION CONTRACTS                        |  |
|  |  - Abstract interfaces for future UI PresenceSurface, ContextProvider,      |  |
|  |    ApplicationAdapter, AgentBackend, and ModelProvider                     |  |
|  +-----------------------------------------------------------------------------+  |
+===================================================================================+
```

---

## 4. DETAILED SUBSYSTEM SPECIFICATIONS

### 4.1 Subsystem A: Bootstrap & Lifecycle Manager
- **Single-Instance Mutex:**  
  On startup, the manager calls Win32 `kernel32.CreateMutexW(None, True, "Local\\PresenceRuntime_SingleInstanceMutex")`.  
  If `kernel32.GetLastError()` returns `ERROR_ALREADY_EXISTS` (183), another instance is already running. The process logs a notice and immediately exits with exit code `2`.
- **Lifecycle States:**
  ```
  [UNINITIALIZED] ──( init() )──> [STARTING] ──( start() )──> [RUNNING]
                                                                  │
                                   [TERMINATED] <──( stop() )─────┘
  ```
- **Controlled Shutdown:** Coordinates shutdown across all registered subsystems in reverse dependency order: stops command listener -> drains event queue -> checkpoints SQLite WAL -> flushes log buffers -> releases named mutex.

### 4.2 Subsystem B: Internal Event System
- **Event Envelope:** Every event is an immutable data object:
  ```python
  @dataclass(frozen=True)
  class Event:
      event_id: str          # UUIDv4
      event_type: str        # e.g., "RuntimeStarted", "CommandReceived"
      timestamp_utc: float   # Epoch timestamp
      sequence_num: int      # Monotonically increasing atomic sequence
      payload: dict[str, Any]# Typed event parameters
  ```
- **Error Isolation:** Subscriber callbacks are executed inside isolated `try/except` blocks. An unhandled exception in an event listener is logged as a critical error but **never** halts the event dispatcher thread or other subscribers.

### 4.3 Subsystem C: Command Dispatcher
- **Deterministic Fast-Path Commands:** Operates without LLM or AI dependency.
- **Initial Supported Commands:**
  1. `status`: Returns runtime uptime, state, memory working set, and subsystem health.
  2. `config [key] [val]`: Inspects or mutates local persistent runtime configuration.
  3. `help`: Lists registered commands and usage.
  4. `shutdown`: Initiates controlled runtime shutdown.
- **Extensibility:** Modules register command handlers via `@dispatcher.register("cmd_name")`.

### 4.4 Subsystem D: Permission & Action Boundary
Enforces the mandatory 4-Tier Authority Gate before any execution handle is granted:

| Level | Name | Description | Policy Enforcement |
| :--- | :--- | :--- | :--- |
| **L0** | **Observe** | Read-only telemetry, config read, health check. | **Autonomous (Always Permitted)** |
| **L1** | **Recommend** | Synthesizing suggestions, preparing diffs. | **Autonomous Presentation** |
| **L2** | **Execute Reversible** | Writing to temporary files, setting runtime config. | **Autonomous within active policy** |
| **L3** | **Protected** | System process kill, credential modification, file deletion. | **Mandatory Interactive User Consent** |

Any tool, command, or future model attempting an action without passing through `PermissionBoundary.authorize(action, level)` is rejected with a `PermissionDeniedError` and logged to the security audit journal.

### 4.5 Subsystem E: Persistence Layer (SQLite WAL)
- **Path:** `%LOCALAPPDATA%\Presence\data\runtime.db` (falls back to local `./data/runtime.db`).
- **Pragmas:**
  ```sql
  PRAGMA journal_mode = WAL;
  PRAGMA synchronous = NORMAL;
  PRAGMA foreign_keys = ON;
  PRAGMA busy_timeout = 5000;
  ```
- **Core Tables:**
  - `runtime_config`: Key-value configuration store with update timestamps.
  - `runtime_recovery`: Stores process ID, start time, heartbeat, and clean-shutdown flag.
  - `system_metrics`: Periodic working set and health metric samples.

### 4.6 Subsystem F: Diagnostics & Logging
- **Path:** `%LOCALAPPDATA%\Presence\logs\runtime.log`.
- **Format:** Structured text and JSON formats with timestamp, log level, subsystem tag, and message.
- **Sanitization:** Built-in regex filters redact API tokens (`Bearer ...`, `sk-...`, `AIza...`), passwords, and credential strings before bytes touch disk.
- **Rotation:** Timed or size-based rotation (max 10 MB per log file, retaining 5 backups).

### 4.7 Subsystem G: Future Integration Contracts
Formal interface definitions (Python `Protocol` / `ABC`) specifying method contracts for future milestones:
- `IPresenceSurface`: Interface for future visual overlays / HUDs.
- `IContextProvider`: Interface for active window / browser context providers.
- `IApplicationAdapter`: Interface for application connectors (Chrome, VS Code).
- `IAgentBackend`: Interface for sandboxed agent runners (OpenShell).
- `IMemoryProvider`: Interface for persistent personal memory.
- `IModelProvider`: Interface for local/cloud LLM routing.
