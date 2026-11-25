# PRESENCE — TASK ORCHESTRATION, CONCURRENCY & MULTI-AGENT EXECUTION
## Phase 2 Implementation Report: Task Management & Reactive Event Wakeup

**Target Subsystem:** `runtime/observability/task_registry.py`, `runtime/persistence.py`, `runtime/tools/task_tools.py`, `runtime/session.py`, `runtime/events.py`  
**Date:** November 19, 2026  
**Status:** COMPLETE (Phase 2 Acceptance Criteria Satisfied)  
**Verification:** 10/10 focused tests passing in `tests/test_process_manager.py` & `tests/test_task_management.py` (6.63s), 39/39 regression tests passing (9.37s)

---

## 1. Executive Summary

Phase 2 elevates background tasks and operating-system subprocesses into first-class, persistent, inspectable, and reactively observable entities across Presence.

Prior to Phase 2:
- `ActiveTaskRegistry` kept task records solely in an in-memory dictionary. If Presence crashed or restarted, all tasks vanished.
- The `TaskType` enum was limited to basic runtime components and lacked types for `SUBPROCESS`, `SUBAGENT`, or `WORKFLOW`.
- The agent lacked native tools to inspect, query, or terminate running background tasks.
- Background tasks completed silently without reactively resuming or notifying the active conversation session.

With Phase 2:
1. **Persistent SQLite Task Storage:** All tasks are recorded transactionally into the `tasks` table in SQLite (`runtime.db`) in WAL mode.
2. **Crash Recovery Reconciliation:** On runtime startup, `PersistenceManager.reconcile_interrupted_tasks()` automatically scans for any tasks left in `RUNNING`, `STARTING`, or `QUEUED` and marks them `INTERRUPTED` with audit timestamps.
3. **Task Type Expansion:** `TaskType` now includes `SUBPROCESS`, `SUBAGENT`, `WORKFLOW`, `WORKFLOW_PHASE`, and `WORKFLOW_STEP`. `TaskState` includes `TIMED_OUT`.
4. **Native Agent Tool (`presence.manage_task`):** Exposes `list`, `status`, `kill`, and `send_input` directly to the model, with live log tailing and process tree termination.
5. **Hierarchical Session Linkage:** `AgentSessionFactory.create_session()` accepts and persists `parent_conversation_id` and `nesting_depth` into `conversation_summaries`.
6. **Reactive Event Wakeup Architecture:** `EventBus` broadcasts `TASK_COMPLETED`, `TASK_FAILED`, and `TASK_CANCELLED`. `AgentSession` captures these events into its notification queue and injects structured updates into the agent's turn context.

---

## 2. Implemented Subsystems & Changes

### 2.1 Persistent Schema (`runtime/persistence.py`)
```sql
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    task_type TEXT NOT NULL,
    display_name TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    started_at REAL,
    completed_at REAL,
    parent_task_id TEXT,
    conversation_id TEXT,
    exit_code INTEGER,
    error_message TEXT,
    artifact_ref TEXT,
    metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);
CREATE INDEX IF NOT EXISTS idx_tasks_conv ON tasks(conversation_id);
```

### 2.2 Dual-Layer Registry (`runtime/observability/task_registry.py`)
`ActiveTaskRegistry` maintains an active in-memory cache for ultra-fast polling and hierarchical tree navigation, while syncing every state transition (`QUEUED` $\rightarrow$ `RUNNING` $\rightarrow$ `COMPLETED` / `FAILED` / `CANCELLED`) atomically to SQLite. `get_task(task_id)` transparently falls back to persistent storage if a task is no longer in RAM.

### 2.3 Native Agent Tool (`runtime/tools/task_tools.py`)
```python
presence.manage_task(
    action: "list" | "status" | "kill" | "send_input",
    task_id: Optional[str] = None,
    input: Optional[str] = None
)
```
- `list`: Returns active tasks, background OS processes, elapsed execution time, and log paths.
- `status`: Returns state, exit code, elapsed duration, artifact references, and the latest reverse-seek stdout/stderr log tails.
- `kill`: Cleanly terminates the task and its entire Windows Job Object process tree.
- `send_input`: Delivers text to standard input (`stdin`) of active processes.

### 2.4 Reactive Wakeup Bridge (`runtime/session.py`, `runtime/agent_executor.py`)
`AgentSession` automatically subscribes to task lifecycle events for its conversation. When a background task completes:
1. `EventBus` broadcasts the typed event outside lock boundaries.
2. `AgentSession` buffers it into `pending_notifications`.
3. At the start of the next turn (or on reactive wake), notifications are drained into `desktop_ambient_context` and injected into the model's system prompt:
   ```text
   Reactive Background Task Updates:
   - [TASK_COMPLETED] Task 'proc_8a3f9e2b1c4d' (Build Project Task) (Artifact: tasks/proc_.../stdout.log)
   ```

---

## 3. Test Verification & Results

All 4 test cases in `tests/test_task_management.py` and all 6 in `tests/test_process_manager.py` passed:

| Test Case | Description | Result |
|-----------|-------------|:------:|
| `test_persistent_task_state_in_sqlite` | Register, start, and complete task with SQLite state & timestamp verification | **PASSED** |
| `test_crash_recovery_reconciliation` | Reconcile orphaned running tasks to INTERRUPTED state on startup | **PASSED** |
| `test_manage_task_tool_actions` | Execute `presence.manage_task` (list, status, kill) on real background sleep command | **PASSED** |
| `test_reactive_wakeup_event_notification` | EventBus publication on completion and session payload verification | **PASSED** |
| `test_process_manager.py` (6 tests) | Synchronous, backgrounding, tailing, kill, and MCP tool execution | **PASSED** |
| `Full Regression Suite` (39 tests) | Session composition, persistence WAL, reconstruction compaction, search governor | **PASSED** |

---

## 4. Phase 2 Gate Review

- **Persistent Task State:** Yes. Transactional SQLite WAL backing.
- **Interrupted Recovery:** Yes. Automatic reconciliation on boot.
- **Agent Self-Inspection:** Yes. `presence.manage_task` tool registered in all sessions.
- **Reactive Wakeup:** Yes. EventBus task events captured and injected into turn context.
- **Zero Regressions:** Yes. All 39 existing regression tests passing cleanly.

**Gate Decision:** Phase 2 Accepted. Ready to proceed to **Phase 3 — Parallel Tool Execution**.
