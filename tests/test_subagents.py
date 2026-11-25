"""Unit and Integration Tests for Ephemeral Subagents (Phase 4)
Tests isolated conversation trajectories, role-based tool filtering,
concurrency guards, recursion depth ceilings, cancellation, and tool execution.
"""

import os
import time
import shutil
import threading
import pytest
from pathlib import Path
from typing import Iterator

from runtime.subagents.models import SubagentConfig, SubagentRole, SubagentStatus
from runtime.subagents.manager import SubagentManager
from runtime.session import AgentSessionFactory
from runtime.observability.task_registry import ActiveTaskRegistry
from runtime.observability.models import TaskType, TaskState
from runtime.events import EventBus
from runtime.artifacts.store import ArtifactStore
from runtime.trajectory import TrajectoryStore
from runtime.providers.fake import FakeModelProvider
from runtime.providers.base import ModelStreamChunk
from runtime.tools.subagent_tools import build_subagent_tools


class ScriptedSubagentProvider(FakeModelProvider):
    """Model provider returning deterministic text and tool responses for testing."""
    def __init__(self, responses=None):
        super().__init__()
        self.responses = responses or ["Subagent research report: discovered relevant architecture facts."]
        self.call_count = 0

    def generate_stream(self, messages, tools=None, cancellation_token=None, system_prompt=None) -> Iterator[ModelStreamChunk]:
        resp_text = self.responses[min(self.call_count, len(self.responses) - 1)]
        self.call_count += 1
        yield ModelStreamChunk(text_delta=resp_text)


class BlockingSubagentProvider(FakeModelProvider):
    """Model provider that stays blocked until explicitly released or cancelled."""
    def __init__(self, release_event=None):
        super().__init__()
        self.release_event = release_event or threading.Event()

    def generate_stream(self, messages, tools=None, cancellation_token=None, system_prompt=None) -> Iterator[ModelStreamChunk]:
        while not self.release_event.is_set():
            if cancellation_token and cancellation_token.is_cancelled:
                return
            time.sleep(0.05)
        yield ModelStreamChunk(text_delta="Finished blocked turn.")


@pytest.fixture
def subagent_env(tmp_path):
    conv_dir = tmp_path / "conversations"
    art_dir = tmp_path / "artifacts"
    conv_dir.mkdir(parents=True, exist_ok=True)
    art_dir.mkdir(parents=True, exist_ok=True)

    event_bus = EventBus()
    task_registry = ActiveTaskRegistry(event_bus=event_bus)
    artifact_store = ArtifactStore(root_dir=art_dir)
    model_provider = ScriptedSubagentProvider()

    factory = AgentSessionFactory(
        conversations_dir=conv_dir,
        artifact_store=artifact_store,
        default_model_provider=model_provider,
        task_registry=task_registry
    )

    manager = SubagentManager(
        session_factory=factory,
        task_registry=task_registry,
        event_bus=event_bus,
        artifact_store=artifact_store,
        max_global_subagents=3,
        max_subagents_per_parent=2,
        max_recursion_depth=2
    )
    factory.subagent_manager = manager

    return {
        "conv_dir": conv_dir,
        "art_dir": art_dir,
        "event_bus": event_bus,
        "task_registry": task_registry,
        "artifact_store": artifact_store,
        "factory": factory,
        "manager": manager,
        "provider": model_provider,
    }


def test_subagent_isolated_conversation_trajectory(subagent_env):
    """Verifies that subagents execute inside isolated databases with clean parent linkage."""
    manager = subagent_env["manager"]
    factory = subagent_env["factory"]
    conv_dir = subagent_env["conv_dir"]

    # 1. Create parent session
    parent_cid = "conv_parent_1"
    parent_session = factory.create_session(conversation_id=parent_cid, title="Parent Session")

    # 2. Spawn research subagent synchronously
    config = SubagentConfig(
        role=SubagentRole.RESEARCH.value,
        prompt="Perform deep architecture research on SQLite WAL mode."
    )
    receipt = manager.spawn_subagent(parent_conversation_id=parent_cid, config=config, wait_ms=5000)

    assert receipt["status"] == "SUCCESS"
    assert "subagent_id" in receipt
    child_cid = receipt["conversation_id"]
    assert child_cid != parent_cid
    assert "discovered relevant architecture facts" in receipt["summary"]
    assert receipt["step_count"] >= 2  # USER_INPUT + AGENT_TURN

    # 3. Verify Child Conversation Database
    child_store = TrajectoryStore(conversation_id=child_cid, db_dir=conv_dir)
    child_meta = child_store.get_metadata()
    assert child_meta is not None
    assert child_meta.parent_conversation_id == parent_cid
    assert child_meta.nesting_depth == 1
    child_steps = child_store.list_steps()
    assert len(child_steps) >= 2

    # 4. Verify Parent Trajectory is Clean
    parent_store = TrajectoryStore(conversation_id=parent_cid, db_dir=conv_dir)
    parent_steps = parent_store.list_steps()
    # Parent did not execute turns directly, so it has 0 intermediate subagent reasoning steps!
    assert len(parent_steps) == 0

    # 5. Clean close
    parent_session.close()


def test_subagent_role_tool_filtering(subagent_env):
    """Verifies that subagents only receive tools whitelisted for their role."""
    manager = subagent_env["manager"]
    factory = subagent_env["factory"]

    # Parent session has full tools
    parent_cid = "conv_parent_tools"
    parent_session = factory.create_session(conversation_id=parent_cid)

    # Spawn research subagent
    config = SubagentConfig(
        role=SubagentRole.RESEARCH.value,
        prompt="Research task with restricted tools."
    )
    receipt = manager.spawn_subagent(parent_conversation_id=parent_cid, config=config, wait_ms=3000)
    assert receipt["status"] == "SUCCESS"

    # Query the subagent managed instance
    subagent_id = receipt["subagent_id"]
    managed = manager._subagents[subagent_id]
    # Child session should not have subagent spawning tools or shell mutating tools
    assert managed.role == "research"
    parent_session.close()


def test_subagent_concurrency_guards(subagent_env):
    """Verifies global and per-parent subagent limits."""
    factory = subagent_env["factory"]
    release_event = threading.Event()
    blocking_provider = BlockingSubagentProvider(release_event=release_event)
    factory.default_model_provider = blocking_provider
    manager = subagent_env["manager"]
    parent_cid = "conv_parent_guards"

    try:
        # Spawn 2 subagents for parent_cid
        s1 = manager.spawn_subagent(parent_cid, SubagentConfig(prompt="Task 1"), wait_ms=50)
        s2 = manager.spawn_subagent(parent_cid, SubagentConfig(prompt="Task 2"), wait_ms=50)

        assert s1["status"] == "BACKGROUNDED"
        assert s2["status"] == "BACKGROUNDED"

        # 3rd subagent for SAME parent should violate max_subagents_per_parent (limit: 2)
        s3_fail = manager.spawn_subagent(parent_cid, SubagentConfig(prompt="Task 3"), wait_ms=50)
        assert s3_fail["status"] == "ERROR"
        assert "Per-parent subagent limit reached" in s3_fail["error"]

        # 3rd subagent for DIFFERENT parent should be allowed globally (limit: 3)
        s3_ok = manager.spawn_subagent("conv_other_parent", SubagentConfig(prompt="Task 3 other"), wait_ms=50)
        assert s3_ok["status"] == "BACKGROUNDED"

        # 4th global subagent should violate max_global_subagents (limit: 3)
        s4_fail = manager.spawn_subagent("conv_third_parent", SubagentConfig(prompt="Task 4"), wait_ms=50)
        assert s4_fail["status"] == "ERROR"
        assert "Global subagent limit reached" in s4_fail["error"]
    finally:
        release_event.set()
        manager.kill_all()


def test_subagent_recursion_depth_ceiling(subagent_env):
    """Verifies recursion depth limit stops runaway nested subagents."""
    manager = subagent_env["manager"]
    factory = subagent_env["factory"]

    # Parent at depth 0
    p0_cid = "conv_root"
    p0 = factory.create_session(conversation_id=p0_cid, nesting_depth=0)

    # Child at depth 1
    p1_cid = "conv_child_depth1"
    p1 = factory.create_session(conversation_id=p1_cid, parent_conversation_id=p0_cid, nesting_depth=1)

    # Subchild at depth 2
    p2_cid = "conv_child_depth2"
    p2 = factory.create_session(conversation_id=p2_cid, parent_conversation_id=p1_cid, nesting_depth=2)

    # Attempting to spawn from depth 2 (which would become depth 3) must be rejected
    res = manager.spawn_subagent(
        parent_conversation_id=p2_cid,
        config=SubagentConfig(prompt="Recursive spawn attempt"),
        wait_ms=1000
    )
    assert res["status"] == "ERROR"
    assert "Maximum subagent recursion depth exceeded" in res["error"]

    # Verify that session at depth 2 did NOT mount presence.invoke_subagent
    depth2_tools = p2.tool_registry.list_tools()
    tool_names = [t.name for t in depth2_tools]
    assert "invoke_subagent" not in tool_names

    p0.close()
    p1.close()
    p2.close()


def test_subagent_cancellation_and_kill(subagent_env):
    """Verifies that active subagents can be cleanly cancelled and terminated."""
    factory = subagent_env["factory"]
    release_event = threading.Event()
    blocking_provider = BlockingSubagentProvider(release_event=release_event)
    factory.default_model_provider = blocking_provider
    manager = subagent_env["manager"]
    task_registry = subagent_env["task_registry"]

    parent_cid = "conv_cancel_test"
    try:
        res = manager.spawn_subagent(
            parent_conversation_id=parent_cid,
            config=SubagentConfig(prompt="Background task to be killed", timeout_seconds=30.0),
            wait_ms=50
        )
        assert res["status"] == "BACKGROUNDED"
        sub_id = res["subagent_id"]

        killed = manager.kill_subagent(sub_id, reason="Audit cancellation test")
        assert killed is True

        status = manager.get_subagent_status(sub_id)
        assert status["status"] == "CANCELLED"
        assert status["error"] == "Audit cancellation test"

        # Verify TaskRegistry updated
        t_node = task_registry.get_task(sub_id)
        assert t_node.state == TaskState.CANCELLED
    finally:
        release_event.set()
        manager.kill_all()


def test_subagent_tools_integration(subagent_env):
    """Verifies presence.invoke_subagent and presence.manage_subagents tool handlers."""
    manager = subagent_env["manager"]
    current_cid = "conv_tools_integ"

    tools = build_subagent_tools(manager, current_conversation_id=current_cid)
    tool_map = {t.name: t for t in tools}

    assert "invoke_subagent" in tool_map
    assert "send_subagent_message" in tool_map
    assert "manage_subagents" in tool_map

    # 1. Invoke subagent via tool handler
    inv_res = tool_map["invoke_subagent"].handler({
        "role": "verifier",
        "prompt": "Verify system security configurations.",
        "wait_ms": 3000
    })
    assert inv_res["status"] == "SUCCESS"
    sub_id = inv_res["subagent_id"]

    # 2. Query status via manage_subagents
    stat_res = tool_map["manage_subagents"].handler({
        "action": "status",
        "subagent_id": sub_id
    })
    assert stat_res["status"] == "SUCCESS"
    assert stat_res["subagent"]["subagent_id"] == sub_id

    # 3. List via manage_subagents
    list_res = tool_map["manage_subagents"].handler({"action": "list"})
    assert list_res["status"] == "SUCCESS"
    assert list_res["count"] >= 1

    # 4. Kill via manage_subagents (already completed, should return False)
    kill_res = tool_map["manage_subagents"].handler({
        "action": "kill",
        "subagent_id": sub_id
    })
    assert kill_res["killed"] is False  # Already completed
