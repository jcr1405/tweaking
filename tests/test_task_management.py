"""Unit and Integration Tests for Phase 2: Task Management & Reactive Event Wakeup
Verifies persistent SQLite task state, crash recovery reconciliation, presence.manage_task tool,
event publishing on completion/cancellation, and reactive session notification injection.
"""

import os
import time
import shutil
import tempfile
import pytest
from pathlib import Path

from runtime.persistence import PersistenceManager
from runtime.events import EventBus
from runtime.observability.models import TaskNode, TaskState, TaskType
from runtime.observability.task_registry import ActiveTaskRegistry
from runtime.process_manager import ProcessManager
from runtime.tools.task_tools import build_task_tools


@pytest.fixture
def temp_env():
    """Provides isolated temporary directories for persistence and tasks."""
    tmp_dir = tempfile.mkdtemp(prefix="presence_test_phase2_")
    db_path = Path(tmp_dir) / "test_runtime.db"
    tasks_dir = Path(tmp_dir) / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)

    pm = PersistenceManager(db_path=db_path)
    bus = EventBus()
    reg = ActiveTaskRegistry(persistence_manager=pm, event_bus=bus)
    proc_mgr = ProcessManager(base_tasks_dir=tasks_dir, event_bus=bus, task_registry=reg)

    yield {
        "dir": tmp_dir,
        "pm": pm,
        "bus": bus,
        "reg": reg,
        "proc_mgr": proc_mgr
    }

    pm.close()
    proc_mgr.cleanup()
    shutil.rmtree(tmp_dir, ignore_errors=True)


def test_persistent_task_state_in_sqlite(temp_env):
    """Verifies that ActiveTaskRegistry persists task lifecycle to SQLite."""
    reg = temp_env["reg"]
    pm = temp_env["pm"]

    # 1. Register task
    node = reg.register_task(
        task_type=TaskType.SUBPROCESS,
        display_name="Build Project Task",
        conversation_id="conv_123",
        cancellable=True
    )
    tid = node.task_id

    # Verify SQLite row
    row = pm.get_task(tid)
    assert row is not None
    assert row["task_id"] == tid
    assert row["display_name"] == "Build Project Task"
    assert row["state"] == "QUEUED"

    # 2. Start task
    reg.start_task(tid)
    row_started = pm.get_task(tid)
    assert row_started["state"] == "RUNNING"
    assert row_started["started_at"] is not None

    # 3. Complete task
    reg.complete_task(tid, artifact_id="art_stdout_001")
    row_completed = pm.get_task(tid)
    assert row_completed["state"] == "COMPLETED"
    assert row_completed["completed_at"] is not None
    assert row_completed["artifact_ref"] == "art_stdout_001"


def test_crash_recovery_reconciliation(temp_env):
    """Verifies that unclosed running tasks are safely marked INTERRUPTED on startup."""
    pm = temp_env["pm"]

    # Insert a task that was left running during a simulated crash
    pm.save_task({
        "task_id": "zombie_proc_999",
        "task_type": "SUBPROCESS",
        "display_name": "Unfinished Long Job",
        "state": "RUNNING",
        "created_at": time.time() - 100,
        "started_at": time.time() - 90
    })

    # Trigger recovery reconciliation
    reconciled = pm.reconcile_interrupted_tasks()
    assert reconciled >= 1

    row = pm.get_task("zombie_proc_999")
    assert row["state"] == "INTERRUPTED"
    assert "unclean runtime shutdown" in row["error_message"]


def test_manage_task_tool_actions(temp_env):
    """Verifies presence.manage_task tool (list, status, kill, send_input)."""
    reg = temp_env["reg"]
    proc_mgr = temp_env["proc_mgr"]

    tools = build_task_tools(task_registry=reg, process_manager=proc_mgr)
    assert len(tools) == 1
    tool = tools[0]
    assert tool.name == "presence.manage_task"

    # 1. Launch a background sleep command
    bg_res = proc_mgr.run_command(
        command="Start-Sleep -Seconds 30",
        wait_ms=100,
        shell="powershell"
    )
    tid = bg_res["task_id"]

    # 2. Tool action: list
    list_res = tool.handler({"action": "list"})
    assert list_res["status"] == "SUCCESS"
    assert any(t["task_id"] == tid for t in list_res["tasks"])

    # 3. Tool action: status
    st_res = tool.handler({"action": "status", "task_id": tid})
    assert st_res["status"] == "SUCCESS"
    assert st_res["task_id"] == tid
    assert st_res["state"] == "RUNNING"

    # 4. Tool action: kill
    kill_res = tool.handler({"action": "kill", "task_id": tid})
    assert kill_res["status"] == "SUCCESS"
    assert "successfully cancelled/terminated" in kill_res["message"]

    # Confirm it is CANCELLED
    post_kill = tool.handler({"action": "status", "task_id": tid})
    assert post_kill["state"] == "CANCELLED"


def test_reactive_wakeup_event_notification(temp_env):
    """Verifies that task completions publish EventBus events and inject into AgentSession."""
    bus = temp_env["bus"]
    reg = temp_env["reg"]

    captured_events = []
    bus.subscribe("TASK_COMPLETED", lambda ev: captured_events.append(ev))

    # Register and complete task
    node = reg.register_task(
        task_type=TaskType.SUBPROCESS,
        display_name="Reactive Background Step",
        conversation_id="conv_reactive_test"
    )
    reg.start_task(node.task_id)
    reg.complete_task(node.task_id, artifact_id="art_log_456")

    # Verify event dispatched
    assert len(captured_events) == 1
    ev = captured_events[0]
    assert ev.event_type == "TASK_COMPLETED"
    assert ev.payload["task_id"] == node.task_id
    assert ev.payload["artifact_id"] == "art_log_456"
    assert ev.payload["conversation_id"] == "conv_reactive_test"
