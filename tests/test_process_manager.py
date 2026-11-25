"""Unit and Integration Tests for Phase 1: Asynchronous Process Manager
Verifies bounded-wait synchronous/asynchronous handoffs, output streaming to disk,
Windows Job Object containment, log tailing, process termination, and MCP integration.
"""

import os
import sys
import time
import shutil
import tempfile
import pytest
from pathlib import Path

from runtime.process_manager.models import ProcessState, ManagedProcess
from runtime.process_manager.output_collector import ProcessOutputCollector
from runtime.process_manager.manager import ProcessManager


@pytest.fixture
def temp_tasks_dir():
    """Provides an isolated temporary directory for process task artifacts."""
    d = tempfile.mkdtemp(prefix="presence_test_tasks_")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


def test_fast_command_synchronous(temp_tasks_dir):
    """Verifies that commands completing within wait_ms return synchronous results."""
    pm = ProcessManager(base_tasks_dir=temp_tasks_dir)
    res = pm.run_command(
        command="Write-Output 'Hello from Presence ProcessManager'",
        wait_ms=5000,
        shell="powershell"
    )

    assert res["status"] == "SUCCESS"
    assert res["exit_code"] == 0
    assert "Hello from Presence ProcessManager" in res["stdout"]
    assert res["task_id"].startswith("proc_")
    assert os.path.exists(res["stdout_path"])
    assert os.path.exists(res["stderr_path"])


def test_fast_command_failure_synchronous(temp_tasks_dir):
    """Verifies that fast commands with nonzero exit codes report failure synchronously."""
    pm = ProcessManager(base_tasks_dir=temp_tasks_dir)
    res = pm.run_command(
        command="exit 42",
        wait_ms=5000,
        shell="powershell"
    )

    assert res["status"] == "FAILED"
    assert res["exit_code"] == 42
    assert res["task_id"].startswith("proc_")


def test_slow_command_backgrounding(temp_tasks_dir):
    """Verifies that commands exceeding wait_ms are backgrounded and supervise cleanly."""
    pm = ProcessManager(base_tasks_dir=temp_tasks_dir)
    # Run a script that sleeps 2 seconds, but set wait_ms to 300ms
    res = pm.run_command(
        command="Start-Sleep -Milliseconds 1500; Write-Output 'Finished Slow Job'",
        wait_ms=300,
        shell="powershell"
    )

    assert res["status"] == "BACKGROUNDED"
    assert "task_id" in res
    task_id = res["task_id"]

    # Immediately inspect status while running
    status = pm.get_process_status(task_id)
    assert status is not None
    assert status["state"] in (ProcessState.RUNNING.value, ProcessState.COMPLETED.value)

    # Wait for the background supervisor to detect completion
    deadline = time.time() + 5.0
    completed = False
    while time.time() < deadline:
        st = pm.get_process_status(task_id)
        if st and st["state"] == ProcessState.COMPLETED.value:
            completed = True
            break
        time.sleep(0.1)

    assert completed, "Background process did not transition to COMPLETED."
    final_status = pm.get_process_status(task_id)
    assert final_status["exit_code"] == 0
    assert "Finished Slow Job" in final_status["stdout_tail"]


def test_large_output_stream_and_tail(temp_tasks_dir):
    """Verifies that large stdout does not deadlock pipes and tails correctly from disk."""
    pm = ProcessManager(base_tasks_dir=temp_tasks_dir)
    # Output 1,000 numbered lines quickly without sleep
    ps_cmd = "1..1000 | ForEach-Object { \"Line $_\" }"
    res = pm.run_command(command=ps_cmd, wait_ms=8000, shell="powershell")

    task_id = res["task_id"]
    # Wait until finished
    deadline = time.time() + 10.0
    while time.time() < deadline:
        st = pm.get_process_status(task_id)
        if st and st["state"] in (ProcessState.COMPLETED.value, ProcessState.FAILED.value):
            break
        time.sleep(0.1)

    final_status = pm.get_process_status(task_id)
    assert final_status["total_bytes_collected"] > 5000
    # Check tail contains the final lines
    tail = final_status.get("stdout_tail", "")
    assert "Line 1000" in tail or "Line 999" in tail


def test_process_cancellation_and_kill(temp_tasks_dir):
    """Verifies that kill_process terminates the process tree and sets CANCELLED state."""
    pm = ProcessManager(base_tasks_dir=temp_tasks_dir)
    # Launch an indefinite sleep
    res = pm.run_command(
        command="Start-Sleep -Seconds 60",
        wait_ms=200,
        shell="powershell"
    )

    assert res["status"] == "BACKGROUNDED"
    task_id = res["task_id"]

    kill_ok = pm.kill_process(task_id, reason="Test cancellation")
    assert kill_ok is True

    st = pm.get_process_status(task_id)
    assert st["state"] == ProcessState.CANCELLED.value
    assert "Test cancellation" in st["error_message"]


def test_shell_server_tools_integration():
    """Verifies MCP shell server tools work through the new ProcessManager layer."""
    from runtime.mcp.servers.shell_server import (
        execute_command,
        get_process_status,
        kill_process,
        list_processes,
        which
    )

    # 1. which tool
    which_res = which("powershell")
    assert which_res["found"] is True

    # 2. execute_command fast
    exec_res = execute_command("Write-Output 'MCP Shell Server OK'", wait_ms=5000)
    assert exec_res["status"] == "SUCCESS"
    assert "MCP Shell Server OK" in exec_res["stdout"]

    # 3. execute_command backgrounded
    bg_res = execute_command("Start-Sleep -Milliseconds 1500; Write-Output 'MCP BG OK'", wait_ms=200)
    assert bg_res["status"] == "BACKGROUNDED"
    tid = bg_res["task_id"]

    # 4. get_process_status
    st = get_process_status(tid)
    assert st.get("status") != "NOT_FOUND"
    assert "state" in st

    # 5. list_processes
    proc_list = list_processes()
    assert proc_list["count"] >= 1
    assert any(p["task_id"] == tid for p in proc_list["processes"])

    # 6. kill_process
    kill_res = kill_process(tid, reason="Clean test finish")
    assert kill_res["terminated"] is True or st["state"] == "COMPLETED"
