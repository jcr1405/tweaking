# FOUNDATION IMPLEMENTATION PLAN
## Milestone 0 — Presence Runtime Foundation
**Document Version:** 1.0.0-PLAN-SPEC  
**Classification:** Engineering Implementation & Test Specification  
**Execution Target:** Python 3.12 Native Windows Substrate  

---

## 1. IMPLEMENTATION SCOPE & WORKSPACE LAYOUT

Milestone 0 delivers the minimal runnable, robust, headless Presence Runtime inside the project workspace (`c:\Users\chait\OneDrive\Documents\projects\tweaking`).

```
tweaking/
├── runtime/
│   ├── __init__.py          # Package initialization & version metadata
│   ├── platform_win32.py    # Native Win32 API bindings (Named Mutex, Memory, Signals)
│   ├── logging.py           # Subsystem F: Structured diagnostics & secret redaction
│   ├── events.py            # Subsystem B: In-memory typed event bus & envelopes
│   ├── permissions.py       # Subsystem D: 4-Tier Authority Boundary & Gatekeeper
│   ├── persistence.py       # Subsystem E: SQLite WAL transactional persistence engine
│   ├── commands.py          # Subsystem C: Deterministic Fast-Path command dispatcher
│   ├── contracts.py         # Subsystem G: Future modular interface contracts (ABCs)
│   ├── lifecycle.py         # Subsystem A: Bootstrap, component registry & state machine
│   └── main.py              # CLI entry point, headless daemon loop & command runner
│
├── tests/
│   ├── __init__.py
│   ├── test_platform_win32.py # Tests for single-instance mutex & Win32 memory queries
│   ├── test_events.py         # Tests for event dispatch, typing & error isolation
│   ├── test_permissions.py    # Tests for 4-tier authority gate & denial enforcement
│   ├── test_persistence.py    # Tests for SQLite WAL mode, config store & transactions
│   ├── test_commands.py       # Tests for deterministic command execution
│   ├── test_lifecycle.py      # Tests for state transitions, clean shutdown & recovery
│   └── run_all.py             # Automated test suite runner aggregating all results
│
└── specs/
    ├── 14_PRESENCE_RUNTIME_ARCHITECTURE.md
    └── 15_FOUNDATION_IMPLEMENTATION_PLAN.md
```

---

## 2. STEP-BY-STEP IMPLEMENTATION ROADMAP

### Step 1: Native Win32 Platform Integration (`runtime/platform_win32.py`)
- Implement `Win32SingleInstanceMutex`: Wraps `kernel32.CreateMutexW` and checks `GetLastError() == ERROR_ALREADY_EXISTS (183)`.
- Implement `get_process_memory_info()`: Wraps `psapi.GetProcessMemoryInfo` (or `K32GetProcessMemoryInfo`) to extract physical Working Set and Private Bytes.
- Implement `set_console_ctrl_handler()`: Registers Win32 console control handler for `CTRL_C_EVENT`, `CTRL_CLOSE_EVENT`, and `CTRL_SHUTDOWN_EVENT`.

### Step 2: Diagnostics & Sanitized Logging (`runtime/logging.py`)
- Configure standard `logging` with structured formatting: `[Timestamp] [Level] [Subsystem] Message`.
- Implement `SanitizingFilter`: Regex scrubbing for API keys (`Bearer ...`, `sk-...`), passwords, and credential strings.
- Configure rotating file handler targeting `%LOCALAPPDATA%\Presence\logs\runtime.log`.

### Step 3: Typed Event Bus (`runtime/events.py`)
- Define frozen dataclass `Event` with `event_id`, `event_type`, `timestamp_utc`, `sequence_num`, `payload`.
- Implement `EventBus`: Thread-safe subscriber registration, synchronous dispatch, and error-isolated handler execution.

### Step 4: Permission & Action Boundary (`runtime/permissions.py`)
- Define `AuthorityLevel` enum: `L0_OBSERVE`, `L1_RECOMMEND`, `L2_EXECUTE_REVERSIBLE`, `L3_PROTECTED`.
- Implement `PermissionBoundary`: Authorizes actions against active policy; raises `PermissionDeniedError` if authorization fails.

### Step 5: Persistence Engine (`runtime/persistence.py`)
- Implement `PersistenceManager`: Opens SQLite database in WAL mode (`runtime.db`).
- Execute schema migrations: `runtime_config`, `runtime_recovery`, `system_metrics`.
- Provide atomic transaction methods for config get/set and recovery state recording.

### Step 6: Command Dispatcher (`runtime/commands.py`)
- Implement `CommandDispatcher`: Routes text commands to deterministic handlers.
- Implement core command set: `status`, `help`, `config`, `shutdown`.

### Step 7: Bootstrap & Lifecycle Manager (`runtime/lifecycle.py`)
- Implement `PresenceRuntime`: Coordinates subsystem instantiation, registration, startup sequencing, heartbeat logging, and graceful termination.

### Step 8: CLI Entry Point & Runner (`runtime/main.py`)
- Parse CLI arguments (`--headless`, `--status`, `--command <cmd>`).
- Start background runtime loop with waitable shutdown event.

### Step 9: Comprehensive Automated Test Suite (`tests/`)
- Unit tests for all individual subsystems.
- Integration tests validating recovery from simulated hard process termination.
- Resource benchmark script measuring idle CPU and Working Set RAM.

---

## 3. DEPENDENCIES & CONSTRAINTS

- **Language:** Python 3.12 (Native Windows 64-bit).
- **External Dependencies:** **ZERO new packages.** Exclusively uses Python standard library (`ctypes`, `sqlite3`, `threading`, `logging`, `dataclasses`, `enum`, `pathlib`, `json`, `sys`, `os`, `signal`) and pre-installed `psutil`.

---

## 4. ACCEPTANCE CRITERIA FOR MILESTONE 0

- [ ] **A1. Clean Bootstrap:** Runtime initializes, registers single-instance mutex, and transitions `CREATED -> STARTING -> RUNNING`.
- [ ] **A2. Duplicate Prevention:** A second concurrent invocation immediately detects the active mutex and exits with code 2 without disturbing the running instance.
- [ ] **A3. Deterministic Commands:** Commands (`status`, `help`, `config`, `shutdown`) execute and return structured output in < 15ms.
- [ ] **A4. Persistence Integrity:** Config settings and recovery state persist across restarts; SQLite executes in WAL mode with zero corruption.
- [ ] **A5. Permission Enforcement:** L3 protected actions cannot execute without explicit consent; unauthorized actions are rejected and audited.
- [ ] **A6. Graceful Shutdown:** `SIGINT` / `shutdown` command triggers reverse teardown, WAL checkpoint, and clean mutex release.
- [ ] **A7. Resource Targets:** Measured idle CPU is **< 0.3%**; measured idle Working Set RAM is **< 50 MB** (well below the 100 MB provisional ceiling).
- [ ] **A8. 100% Test Pass:** All automated unit and integration tests pass cleanly via `python -m unittest`.
