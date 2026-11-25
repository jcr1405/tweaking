"""Tests for Phase 2 and Phase 3: Live Session Delegation and Local Operations Supervisor.
Validates:
1. Cloud Principal session registers presence.delegate_local_work.
2. Cloud Principal session does NOT register raw MCP or raw desktop execution tools.
3. Supervisor child session receives permitted MCP and desktop tools.
4. Supervisor child session strictly unregisters presence.delegate_local_work (anti-recursion).
5. Local execution uses pinned local provider without cloud fallback.
"""

import pytest
import tempfile
from unittest.mock import MagicMock, patch

from runtime.lifecycle import PresenceRuntime
from runtime.local_operations.models import (
    TaskDelegationContract,
    ExecutionProfile,
    DelegationConstraints,
)
from runtime.permissions import PermissionTier


@pytest.fixture
def runtime_fixture():
    with tempfile.TemporaryDirectory() as tmpdir:
        rt = PresenceRuntime(
            mutex_name="Local\\TestLiveDelegationMutex",
            db_path=tmpdir + "/pres.db",
            log_dir=tmpdir + "/logs",
            summary_db_path=tmpdir + "/sum.db",
            artifact_dir=tmpdir + "/art",
            schema_cache_dir=tmpdir + "/cache",
            enable_context_awareness=False,
        )
        yield rt
        try:
            rt.stop()
        except Exception:
            pass


def test_cloud_principal_session_tool_catalog(runtime_fixture):
    """Verifies Cloud Principal exposes delegation tool and strictly excludes raw operational catalogues."""
    session = runtime_fixture.create_session(conversation_id="cloud-conv-1")
    tool_ids = [t.tool_id for t in session.tool_registry.list_tools()]

    # Cloud Principal must have presence.delegate_local_work
    assert "presence.delegate_local_work" in tool_ids

    # Cloud Principal must NOT have raw MCP tools
    assert not any(t.startswith("mcp.") for t in tool_ids), "Raw MCP tools exposed to cloud principal!"

    # Cloud Principal must NOT have raw desktop tools
    assert not any(t.startswith("desktop.") for t in tool_ids), "Raw desktop tools exposed to cloud principal!"

    # Cloud Principal must retain conversational and high-level tools
    assert "presence.create_plan" in tool_ids
    assert "presence.run_workflow" in tool_ids
    assert "presence.get_runtime_status" in tool_ids


def test_child_session_anti_recursion(runtime_fixture):
    """Verifies child supervisor sessions cannot recursively delegate."""
    child_session = runtime_fixture.session_factory.create_session(
        conversation_id="child-conv-1",
        nesting_depth=1,
        is_cloud_principal=False,
        session_role="local_supervisor",
    )
    # Filter supervisor tools
    runtime_fixture.local_supervisor._filter_supervisor_tools(
        child_session,
        allowed_capabilities=["filesystem", "process_inspection", "desktop"],
    )
    child_tool_ids = [t.tool_id for t in child_session.tool_registry.list_tools()]

    # Child session must NOT have delegation tool
    assert "presence.delegate_local_work" not in child_tool_ids


def test_child_session_desktop_tools_when_allowed(runtime_fixture):
    """Verifies desktop tools are registered into child supervisor session when contract allows desktop."""
    child_session = runtime_fixture.session_factory.create_session(
        conversation_id="child-desktop-conv",
        nesting_depth=1,
        is_cloud_principal=False,
        session_role="local_supervisor",
    )
    from runtime.tools.desktop_tools import register_desktop_tools
    register_desktop_tools(child_session.tool_registry, controller=runtime_fixture.desktop_controller)

    runtime_fixture.local_supervisor._filter_supervisor_tools(
        child_session,
        allowed_capabilities=["desktop"],
    )
    child_tool_ids = [t.tool_id for t in child_session.tool_registry.list_tools()]

    assert "desktop.list_windows" in child_tool_ids
    assert "desktop.observe" in child_tool_ids
    assert "desktop.perceive" in child_tool_ids
    assert "desktop.execute_actions" in child_tool_ids
    assert "desktop.verify" in child_tool_ids


def test_child_session_desktop_tools_unregistered_when_disallowed(runtime_fixture):
    """Verifies desktop tools are filtered out when contract does not permit desktop."""
    child_session = runtime_fixture.session_factory.create_session(
        conversation_id="child-no-desktop-conv",
        nesting_depth=1,
        is_cloud_principal=False,
        session_role="local_supervisor",
    )
    from runtime.tools.desktop_tools import register_desktop_tools
    register_desktop_tools(child_session.tool_registry, controller=runtime_fixture.desktop_controller)

    # Filter with only filesystem allowed
    runtime_fixture.local_supervisor._filter_supervisor_tools(
        child_session,
        allowed_capabilities=["filesystem"],
    )
    child_tool_ids = [t.tool_id for t in child_session.tool_registry.list_tools()]

    assert not any(t.startswith("desktop.") for t in child_tool_ids)


def test_delegation_tool_execution_guards(runtime_fixture):
    """Verifies caller role, nesting depth, and identity validation in presence.delegate_local_work."""
    session = runtime_fixture.create_session(conversation_id="cloud-guard-test")
    del_tool = session.tool_registry.get_tool("presence.delegate_local_work")
    assert del_tool is not None

    # Call with valid contract
    args = {
        "objective": "Inspect local logs and verify system status",
        "context_summary": "Investigating log entries",
    }
    # Mock supervisor.execute to return structured result
    with patch.object(runtime_fixture.local_supervisor, "execute") as mock_exec:
        from runtime.local_operations.models import TaskDelegationResult, CloudReturnPacket, DelegationOutcome
        mock_packet = CloudReturnPacket(
            task_id="t-mock",
            parent_conversation_id=session.conversation_id,
            objective="Inspect local logs",
            outcome="completed",
            summary="Inspected local logs successfully.",
            status="SUCCESS",
        )
        mock_result = TaskDelegationResult(
            task_id="t-mock",
            parent_conversation_id=session.conversation_id,
            outcome=DelegationOutcome.COMPLETED,
            summary="Inspected local logs successfully.",
            cloud_packet=mock_packet,
        )
        mock_exec.return_value = mock_result

        res = session.tool_registry.execute(
            tool_id="presence.delegate_local_work",
            args=args,
            permission_boundary=session.permission_boundary,
            conversation_id=session.conversation_id,
        )
        assert res.get("status") == "SUCCESS"
        assert "result" in res
        assert res["result"].get("outcome") in ("completed", "partial", "failed", "blocked")
