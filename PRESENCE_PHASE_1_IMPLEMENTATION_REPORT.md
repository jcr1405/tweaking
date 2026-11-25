# PRESENCE — TASK ORCHESTRATION, CONCURRENCY & MULTI-AGENT EXECUTION
## Phase 1 Implementation Report: Asynchronous Process Manager

**Target Subsystem:** `runtime/process_manager/`, `runtime/mcp/servers/shell_server.py`  
**Date:** November 19, 2026  
**Status:** COMPLETE (Phase 1 Acceptance Criteria Satisfied)  
**Verification:** 6/6 tests passing in `tests/test_process_manager.py` (6.70s)

---

## 1. Executive Summary

Phase 1 eliminates synchronous process blocking in Presence when executing shell commands and operating-system tasks. 

Previously, `runtime/mcp/servers/shell_server.py` invoked synchronous, uncontained `subprocess.run()` calls with up to 120-second blocking timeouts. This froze the calling thread, provided zero progress visibility during long operations (e.g. `npm install`, `cargo build`, `pytest`), risked pipe deadlocks when outputs exceeded OS buffer capacities, and leaked orphaned child processes if Presence crashed or was closed.

With Phase 1, all command execution is governed by `ProcessManager` backed by:
1. **Windows Job Object Containment:** Processes are created suspended, bound to `Win32JobObject` with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, and verified fail-closed via `IsProcessInJob`.
2. **Bounded Synchronous/Asynchronous Handoff:** Commands wait synchronously for up to `wait_ms` (default 5000ms). If they finish within that window, immediate results are returned. If they exceed it, they are seamlessly backgrounded and a task receipt is returned immediately.
3. **Non-Blocking Disk Streaming:** `ProcessOutputCollector` uses background reader threads that drain OS pipes continuously into persistent disk artifacts (`stdout.log`, `stderr.log`), eliminating OS pipe deadlocks and keeping RAM usage strictly bounded.
4. **Log Tailing via Reverse Seek:** Tail queries inspect the last N lines directly from disk via reverse seeking, avoiding reading multi-megabyte log files into memory.
5. **Clean Process Tree Termination:** `kill_process` terminates the entire Windows Job Object tree, leaving zero orphaned processes.

---

## 2. Implemented Components

```text
runtime/process_manager/
├── __init__.py           # Public exports (ProcessManager, ManagedProcess, ProcessState, ProcessOutputCollector)
├── models.py             # ProcessState enum, ManagedProcess dataclass with lifecycle accounting
├── output_collector.py   # Multi-threaded pipe drainer, bounded memory preview, reverse-seek disk tail
└── manager.py            # Bounded wait dispatcher, Job Object supervisor, background monitor, cleanup
```

### 2.1 Process Lifecycle States (`models.py`)
Explicit state machine:
```text
QUEUED ──> STARTING ──> RUNNING ──┬──> COMPLETED (exit code 0)
                                 ├──> FAILED (exit code != 0)
                                 ├──> CANCEL_REQUESTED ──> CANCELLED
                                 ├──> TIMED_OUT
                                 └──> INTERRUPTED
```

### 2.2 Bounded Wait & Execution Receipts (`manager.py`)
```python
def run_command(
    command: str,
    args: Optional[List[str]] = None,
    wait_ms: int = 5000,
    is_daemon: bool = False,
    cwd: Optional[str] = None,
    conversation_id: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    timeout_seconds: Optional[float] = None,
    shell: str = "powershell",
) -> Dict[str, Any]:
```

- **Synchronous Return (<= wait_ms):**
  ```json
  {
    "status": "SUCCESS",
    "task_id": "proc_8a3f9e2b1c4d",
    "pid": 54210,
    "exit_code": 0,
    "duration_seconds": 0.412,
    "stdout": "...",
    "stderr": "",
    "stdout_path": "C:\\Users\\...\\Presence\\tasks\\proc_...\\stdout.log",
    "stderr_path": "C:\\Users\\...\\Presence\\tasks\\proc_...\\stderr.log",
    "total_bytes": 142
  }
  ```

- **Asynchronous Return (> wait_ms):**
  ```json
  {
    "status": "BACKGROUNDED",
    "task_id": "proc_8a3f9e2b1c4d",
    "pid": 54210,
    "message": "Command continues running in background (exceeded 5000ms wait threshold).",
    "wait_ms": 5000,
    "stdout_preview": "...",
    "stderr_preview": "",
    "stdout_path": "C:\\Users\\...\\Presence\\tasks\\proc_...\\stdout.log",
    "stderr_path": "C:\\Users\\...\\Presence\\tasks\\proc_...\\stderr.log"
  }
  ```

### 2.3 Shell Server MCP Integration (`runtime/mcp/servers/shell_server.py`)
- `execute_command`: Upgraded with `wait_ms`, `is_daemon`, and bounded wait delegation.
- `get_process_status`: Returns live execution status, exit code, elapsed duration, and tail lines from disk.
- `kill_process`: Terminates running process and its complete Windows Job Object tree.
- `send_process_input`: Sends text input to active process stdin.
- `list_processes`: Discovers active and recently completed processes.
- `run_powershell_script`: Bound to `ProcessManager` with Job Object containment.

---

## 3. Test Verification & Results

All 6 test cases in `tests/test_process_manager.py` passed cleanly:

| Test Case | Objective | Result | Duration |
|-----------|-----------|:------:|:--------:|
| `test_fast_command_synchronous` | Fast command completes within wait_ms, captures stdout, exit code 0 | **PASSED** | 0.81s |
| `test_fast_command_failure_synchronous` | Fast command with nonzero exit code returns status FAILED | **PASSED** | 0.58s |
| `test_slow_command_backgrounding` | Command exceeding wait_ms transitions to BACKGROUNDED, supervised to COMPLETED | **PASSED** | 2.12s |
| `test_large_output_stream_and_tail` | 1,000 lines streamed without pipe deadlock, disk tail query verified | **PASSED** | 1.15s |
| `test_process_cancellation_and_kill` | Indefinite command terminated via Job Object, state marked CANCELLED | **PASSED** | 0.42s |
| `test_shell_server_tools_integration` | MCP tools (`execute_command`, `get_process_status`, `list_processes`, `kill_process`) | **PASSED** | 1.62s |

---

## 4. Phase 1 Gate Review

- **One PresenceRuntime:** Yes. Uses existing singleton instances.
- **Fail-Closed Containment:** Yes. `Win32SuspendedLauncher` checks `IsProcessInJob`.
- **No Pipe Deadlocks:** Yes. Background collectors drain pipes continuously.
- **Artifact-Backed:** Yes. Logs stored on disk at `tasks/<task_id>/stdout.log`.
- **Zero Orphan Leakage:** Yes. `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` terminates entire process trees.

**Gate Decision:** Phase 1 Accepted. Ready to proceed to **Phase 2 — Task Management & Reactive Event Wakeup**.
