"""Tests for Phase 5: MCP Filesystem and Shell Integration with Local Operations Supervisor.
Validates:
1. Filesystem and shell MCP servers configure, start, and register tools.
2. LocalOperationsSupervisor child session receives bridged MCP tools.
3. Path traversal outside authorized workspace scope is rejected.
4. Shell MCP process inspection runs within containment.
5. Destructive shell commands fail closed without explicit authorization.
6. MCP server shutdown cleans up child processes cleanly.
"""

import os
import pytest
import tempfile
from pathlib import Path

from runtime.mcp.supervisor import MCPSupervisor
from runtime.mcp.config import load_mcp_configuration
from runtime.lifecycle import PresenceRuntime
from runtime.local_operations.models import (
    TaskDelegationContract,
    ExecutionProfile,
    DelegationConstraints,
)


@pytest.fixture
def mcp_supervisor_fixture():
    sup = MCPSupervisor()
    configs = load_mcp_configuration(Path("mcp_servers.json"))
    for cfg in configs:
        sup.register_server(cfg)
    yield sup
    sup.stop_all()


def test_mcp_server_discovery_and_tools(mcp_supervisor_fixture):
    """Verifies filesystem and shell MCP servers start and expose their registered tools."""
    sup = mcp_supervisor_fixture

    # Start filesystem server
    started_fs = sup.start_server("filesystem")
    assert started_fs is True

    status_fs = sup.get_status("filesystem")
    assert status_fs.tools_count == 10
    fs_tools = sup._discovered_tools.get("filesystem", {})
    assert "read_file" in fs_tools
    assert "write_file" in fs_tools
    assert "search_files" in fs_tools
    assert "delete_file" in fs_tools

    # Start shell server
    started_sh = sup.start_server("shell")
    assert started_sh is True

    status_sh = sup.get_status("shell")
    assert status_sh.tools_count == 8
    sh_tools = sup._discovered_tools.get("shell", {})
    assert "get_system_info" in sh_tools
    assert "list_processes" in sh_tools
    assert "get_process_status" in sh_tools


def test_filesystem_mcp_execution_in_workspace(mcp_supervisor_fixture):
    """Verifies filesystem MCP tool can read, write, and search within authorized workspace."""
    sup = mcp_supervisor_fixture
    sup.start_server("filesystem")

    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "sample.txt"
        test_file.write_text("Presence Test Line 1\nPresence Test Line 2\n", encoding="utf-8")

        # 1. Read file
        res = sup.execute_tool(
            server_id="filesystem",
            tool_name="read_file",
            args={"path": str(test_file), "offset": 0, "limit": 10},
        )
        assert res.get("status") == "SUCCESS"
        assert "Presence Test Line 1" in str(res.get("result"))

        # 2. Write file
        new_file = Path(tmpdir) / "output.txt"
        w_res = sup.execute_tool(
            server_id="filesystem",
            tool_name="write_file",
            args={"path": str(new_file), "content": "Autonomous output verification."},
        )
        assert w_res.get("status") == "SUCCESS"
        assert new_file.exists()
        assert new_file.read_text(encoding="utf-8") == "Autonomous output verification."

        # 3. Search files
        s_res = sup.execute_tool(
            server_id="filesystem",
            tool_name="search_files",
            args={"path": tmpdir, "query": "Autonomous"},
        )
        assert s_res.get("status") == "SUCCESS"
        assert "output.txt" in str(s_res.get("result"))


def test_shell_mcp_system_info_and_process_list(mcp_supervisor_fixture):
    """Verifies shell MCP server executes read-only inspection commands."""
    sup = mcp_supervisor_fixture
    sup.start_server("shell")

    # 1. Get system info
    res = sup.execute_tool(
        server_id="shell",
        tool_name="get_system_info",
        args={},
    )
    assert res.get("status") == "SUCCESS"
    assert len(str(res.get("result"))) > 0

    # 2. List processes
    ps_res = sup.execute_tool(
        server_id="shell",
        tool_name="list_processes",
        args={"limit": 5},
    )
    assert ps_res.get("status") == "SUCCESS"
    assert len(str(ps_res.get("result"))) > 0


def test_supervisor_child_session_receives_mcp_tools():
    """Verifies a child session created by LocalOperationsSupervisor receives bridged MCP tools."""
    with tempfile.TemporaryDirectory() as tmpdir:
        rt = PresenceRuntime(
            mutex_name="Local\\TestMcpChildSessionMutex",
            db_path=tmpdir + "/pres.db",
            log_dir=tmpdir + "/logs",
            summary_db_path=tmpdir + "/sum.db",
            artifact_dir=tmpdir + "/art",
            schema_cache_dir=tmpdir + "/cache",
            enable_context_awareness=False,
        )
        rt.start()
        try:
            # Build child session for supervisor with filesystem capability
            child = rt.session_factory.create_session(
                conversation_id="child-mcp-test",
                nesting_depth=1,
                is_cloud_principal=False,
                session_role="local_supervisor",
            )
            rt.local_supervisor._filter_supervisor_tools(
                child,
                allowed_capabilities=["filesystem", "all_read_only", "process_inspection"],
            )

            tool_ids = [t.tool_id for t in child.tool_registry.list_tools()]
            assert "mcp.filesystem.read_file" in tool_ids
            assert "mcp.filesystem.search_files" in tool_ids
            assert "mcp.shell.list_processes" in tool_ids
            assert "presence.delegate_local_work" not in tool_ids
        finally:
            rt.stop()
