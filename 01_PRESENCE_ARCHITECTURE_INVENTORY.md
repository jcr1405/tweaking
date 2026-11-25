# 01 — PRESENCE ARCHITECTURAL INVENTORY & REPOSITORY DISCOVERY REPORT
## Forensic Codebase Inspection, Component Wiring, Lifecycle Sequencing, and Dependency Topography

**Document ID:** PRESENCE-CAPSTONE-AUDIT-01  
**Project:** Presence — Windows-native persistent AI desktop companion  
**Audit Phase:** Phase 1 — Project Discovery and Architectural Inventory  
**Runtime:** Python 3.12.10 (x64) on Windows 11 Enterprise  
**Repository Root:** `c:\Users\chait\OneDrive\Documents\projects\tweaking`  
**Git Branch:** `implementation/phase-2c-context-and-foreground-awareness`  
**Date:** 2026-11-20  

---

## 1. Executive Summary & Repository Baseline

This document establishes the verified forensic inventory of the Presence codebase. Every claim in this document is derived from direct source code inspection, Win32 system diagnostics, and static analysis of the 107 Python modules comprising the runtime.

Presence is engineered as a persistent, resource-efficient Windows desktop companion. It blends an event-driven, multi-process runtime with a deterministic DAG workflow engine (Phase 5), a Task Decomposition and Execution Ledger (TDEL), an ephemeral subagent subsystem (Phase 4), an external MCP supervisor, and a Textual Terminal User Interface (TUI).

### Key Repository Metrics
- **Authoritative Root:** `c:\Users\chait\OneDrive\Documents\projects\tweaking`
- **Total Source Modules (`runtime/`):** 107 Python files
- **Total Lines of Python Code (`runtime/`):** 22,741 lines across 12 distinct subsystems
- **Git Working Tree:** 19 modified files, 51 untracked files (Phases 3–6 implementations, test suites, and documentation). No remote configured.
- **Python Environment:** Python 3.12.10 x64 virtual environment (`.venv`) with 120+ installed packages (notably `textual`, `pydantic`, `google-genai`, `mcp`, `psutil`, `pytest`, `tiktoken`).

---

## 2. Subsystem Topography & Directory Breakdown

```text
c:\Users\chait\OneDrive\Documents\projects\tweaking\
├── runtime/                             # 22,741 lines across 107 files
│   ├── artifacts/                       # ArtifactStore: file offloading, content hashing (3 files, 401 lines)
│   ├── context/                         # Foreground window tracking & snapshotting (8 files, 1,409 lines)
│   ├── mcp/                             # MCPSupervisor, SchemaCache, process containment (10 files, 2,226 lines)
│   ├── observability/                   # ActiveTaskRegistry, metrics, telemetry (6 files, 1,550 lines)
│   ├── process_manager/                 # Win32 Job Object process containment (4 files, 768 lines)
│   ├── providers/                       # Gemini, FreeLLMAPI, Fake providers (5 files, 903 lines)
│   ├── search/                          # SearchGovernor: rate limiting, circuit breaker (9 files, 1,953 lines)
│   ├── subagents/                       # Ephemeral SubagentManager & isolation (3 files, 660 lines)
│   ├── task_planning/                   # TDEL: Classifier, Planner, Ledger, Projection (10 files, 1,828 lines)
│   ├── tools/                           # ToolRegistry & specialized tool packages (7 files, 1,192 lines)
│   ├── tui/                             # Textual TUI: App, Views, Widgets (13 files, 2,027 lines)
│   ├── workflows/                       # WorkflowEngine, Scheduler, Validator (12 files, 1,742 lines)
│   ├── lifecycle.py                     # PresenceRuntime lifecycle coordinator
│   ├── session.py                       # AgentSession composition & preemption
│   ├── permissions.py                   # 4-tier PermissionBoundary
│   ├── persistence.py                   # SQLite WAL persistence engine
│   ├── reconstruction.py                # ContextAssembler & compaction
│   └── main.py                          # CLI & Daemon entrypoint
├── tests/                               # 388 test cases across unit, adversarial, integration suites
└── mcp_servers.json                     # External MCP server definitions (filesystem, shell)
```

---

## 3. Subsystem Breakdown & Architecture Inventory

### 3.1 Lifecycle & Instance Isolation (`runtime/lifecycle.py`)
- **Win32 Mutex Guard:** Enforces single-instance execution via `Win32SingleInstanceMutex` (`Local\PresenceRuntime_SingleInstanceMutex`). A secondary instance exits immediately with exit code 2.
- **Unclean Shutdown Detection:** Inspects SQLite `runtime_recovery` table on startup. If a previous instance PID died without a recorded `record_clean_shutdown`, it logs a warning and invokes reconciliation routines.
- **Heartbeat Thread:** Runs a background daemon updating `runtime_recovery.last_heartbeat_utc` every 5.0 seconds.
- **Ordered Termination Sequence:** Shuts down components in reverse dependency order:
  1. Sets `_shutdown_event`
  2. Stops foreground context monitor (`foreground_source.stop_sync`)
  3. Kills all running subagents (`subagent_manager.kill_all()`)
  4. Cleans up Win32 child processes (`process_manager.cleanup()`)
  5. Terminates external MCP server processes (`mcp_supervisor.stop_all()`)
  6. Closes summary manager and search governor
  7. Publishes `RuntimeStopped` on `EventBus`
  8. Records clean shutdown in SQLite WAL and closes database
  9. Releases Win32 mutex and transitions to `TERMINATED`.

### 3.2 Security & Authority Framework (`runtime/permissions.py`)
- **Authority Lattice:** 4 explicit tiers:
  - `L0_OBSERVE`: Read-only queries, status inspection, search, artifact reads. Idempotent and side-effect free.
  - `L1_RECOMMEND`: Suggesting plans, generating configurations, proposing edits.
  - `L2_EXECUTE_REVERSIBLE`: Reversible operations (creating files, launching processes, invoking subagents, cancelling plans).
  - `L3_PROTECTED`: Destructive, irreversible actions (deleting files, terminating external system processes, wiping databases).
- **Enforcement Point:** Every tool invocation in `ToolRegistry.execute()` and `AgentExecutor` validates that the session's active ceiling meets or exceeds the tool's required tier.

### 3.3 Persistence & Storage Architecture (`runtime/persistence.py`)
- **Engine:** SQLite 3 with strict WAL pragmas:
  - `PRAGMA journal_mode = WAL;`
  - `PRAGMA synchronous = NORMAL;`
  - `PRAGMA foreign_keys = ON;`
  - `PRAGMA busy_timeout = 5000;`
- **Location:** Authoritative runtime database resides at `C:\Users\chait\AppData\Local\Presence\data\runtime.db`.
- **Authoritative Tables:**
  - `runtime_config`: Key-value configuration store
  - `runtime_recovery`: Heartbeat, instance tracking, and crash detection
  - `system_metrics`: Periodic CPU, RAM, and thread metrics
  - `tasks`: Active and historical task registry
  - `workflows` & `workflow_nodes`: Phase 5 deterministic DAG execution runs
  - `task_plans`: TDEL plan ledger with immutable JSON payloads, definition hashes, and status states.

### 3.4 Process & Containment Engine (`runtime/process_manager/`)
- **Win32 Job Object Isolation:** Background processes are assigned to Windows Job Objects with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` configured. When Presence exits, all child processes are automatically reaped by the OS kernel.
- **Memory & Concurrency Bounds:** Limits concurrent subprocesses and throttles output capture.
- **Output Offloading:** Process standard output exceeding 2,000 bytes is automatically offloaded to `ArtifactStore` rather than kept in memory.

### 3.5 Model Providers (`runtime/providers/`)
- **Adapters:**
  - `GeminiModelProvider`: Google GenAI SDK (`google-genai==3.3.0`) targeting `gemini-3.5-flash-lite` (default). Supports streaming deltas and tool declarations.
  - `FreeLLMAPIProvider`: Multi-upstream fallback router (`openrouter`, `cloudflare`, `gemini-free`).
  - `FakeModelProvider`: Offline deterministic testing oracle.
- **Configuration Validation:** Validates API keys and network readiness before initiating session streaming.

### 3.6 Tool Registration Architecture (`runtime/tools/`)
- Tools are organized by functional domain and registered into session-isolated `ToolRegistry` instances:
  - `presence.get_runtime_status`, `presence.get_conversation_info`: Core diagnostics (`L0_OBSERVE`)
  - `presence.web_search`, `presence.fetch_page`: Search governor (`L0_OBSERVE`)
  - `presence.manage_task`: Active background task inspection and termination (`task_tools.py`)
  - `presence.manage_plan`: Read-only TDEL plan inspection (`plan_tools.py`, `L0_OBSERVE`)
  - `presence.cancel_plan`: Mutating plan cancellation (`plan_tools.py`, `L2_EXECUTE_REVERSIBLE`)
  - `presence.invoke_subagent`, `presence.manage_subagents`: Subagent controls (`subagent_tools.py`)
  - `presence.execute_workflow`: Deterministic DAG launcher (`workflow_tools.py`)

---

## 4. Architectural Relationships & Component Communication

```mermaid
graph TD
    User([User / TUI / CLI]) --> Main[runtime/main.py]
    Main --> Runtime[PresenceRuntime]
    
    subgraph Core Infrastructure
        Runtime --> Mutex[Win32SingleInstanceMutex]
        Runtime --> Bus[EventBus]
        Runtime --> Perm[PermissionBoundary]
        Runtime --> Persist[PersistenceManager: SQLite WAL]
        Runtime --> ArtStore[ArtifactStore]
        Runtime --> TaskReg[ActiveTaskRegistry]
    end

    subgraph Execution Subsystems
        Runtime --> ProcMgr[ProcessManager: Job Objects]
        Runtime --> MCPSup[MCPSupervisor: External Servers]
        Runtime --> Gov[SearchGovernor: Leaky Bucket]
        Runtime --> SubMgr[SubagentManager: Ephemeral Workers]
        Runtime --> WFEngine[WorkflowEngine: DAG Scheduler]
        Runtime --> TDEL[TaskPlanningService: Ledger]
    end

    subgraph Conversational Engine
        Runtime --> Factory[AgentSessionFactory]
        Factory --> Session[AgentSession]
        Session --> TDELPreempt{TDEL Preemption?}
        TDELPreempt -- Yes --> TDEL
        TDELPreempt -- No --> Executor[AgentExecutor]
        Executor --> Assembler[ContextAssembler]
        Executor --> ToolReg[ToolRegistry]
        Executor --> Providers[Gemini / FreeLLMAPI]
    end

    TDEL --> WFEngine
    WFEngine --> ProcMgr
    WFEngine --> ToolReg
    WFEngine --> SubMgr
    WFEngine --> Persist
```

---

## 5. Structural Observations, Coupling & Potential Weaknesses

### 5.1 Verification of Dead Code & Import Cycles
- **Import Cycles:** Static analysis verified **0 circular imports** across all 107 modules. All modules import cleanly without runtime lockups.
- **Syntax / Parse Errors:** 0 parse errors across all 22,741 lines of Python code.

### 5.2 Identified Architectural Disconnections & Overlaps
1. **Tool Overlap (`presence.manage_task` vs `presence.manage_plan`):**
   - `manage_task` operates on low-level background threads and OS child processes (`ActiveTaskRegistry`, `ProcessManager`).
   - `manage_plan` operates on high-level multi-step TDEL execution plans and DAG workflows (`TaskExecutionLedger`).
   - *Status:* Cleanly segregated, but user documentation must make clear that a "task" is an OS process while a "plan" is an orchestrated DAG.
2. **Context Awareness (`runtime/context/`):**
   - The native foreground window tracking subsystem (1,409 lines) is completely functional but remains inactive unless `--context-awareness` is explicitly passed on the CLI. It does not activate by default in `--tui` mode.
3. **Provider Fallback Layering:**
   - `PresenceRuntime` defaults to `GeminiModelProvider` with fallback to `FreeLLMAPIProvider`, and finally `FakeModelProvider` when no keys exist. However, dynamic mid-turn provider failover within an active streaming response is not yet implemented. If Gemini throws a 429 mid-stream, the turn fails rather than falling back dynamically to FreeLLMAPI.
4. **Git Repository State:**
   - 19 modified files and 51 untracked files in the working directory. No git remote is attached (`git remote -v` returned empty), meaning the local disk is the sole copy of record.

---

## 6. Acceptance Gate & Phase 1 Sign-Off

Phase 1 Project Discovery and Architectural Inventory is **COMPLETED**.
All runtime components, entrypoints, and communication channels have been forensically verified and mapped.
