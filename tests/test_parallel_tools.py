"""Unit and Integration Tests for Phase 3: Parallel Tool Execution
Verifies tool concurrency metadata, concurrent execution of independent read-only tools,
deterministic trajectory ordering, serial barriers for mutating tools, and error isolation.
"""

import os
import time
import shutil
import tempfile
import pytest
from pathlib import Path
from typing import Dict, Any, List

from runtime.permissions import PermissionBoundary, PermissionTier
from runtime.tools.registry import ToolRegistry, ToolDefinition
from runtime.agent_executor import AgentExecutor, CancellationToken
from runtime.trajectory import TrajectoryStore, StepType, StepStatus
from runtime.journal import EventJournal
from runtime.summaries import GlobalSummaryManager
from runtime.providers.base import IModelProvider, ModelStreamChunk


class MockParallelProvider(IModelProvider):
    """Mock model provider that yields scripted tool calls and responses."""
    def __init__(self, turns_plan: List[Dict[str, Any]]):
        self.turns_plan = turns_plan
        self.call_count = 0

    @property
    def provider_name(self) -> str:
        return "mock_parallel"

    @property
    def model_id(self) -> str:
        return "mock_model"

    def validate_configuration(self):
        return True, "Mock valid"

    def generate_stream(self, system_prompt, messages, tools=None, cancellation_token=None):
        if self.call_count < len(self.turns_plan):
            plan = self.turns_plan[self.call_count]
            self.call_count += 1
            if "tool_calls" in plan:
                yield ModelStreamChunk(tool_calls=plan["tool_calls"])
            if "text" in plan:
                yield ModelStreamChunk(text_delta=plan["text"])
        else:
            yield ModelStreamChunk(text_delta="Default completed.")


@pytest.fixture
def temp_agent_env():
    """Provides isolated trajectory store, journal, and summary manager."""
    tmp_dir = tempfile.mkdtemp(prefix="presence_parallel_test_")
    db_dir = Path(tmp_dir) / "conversations"
    db_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = Path(tmp_dir) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    store = TrajectoryStore(conversation_id="conv_parallel_001", db_dir=db_dir)
    journal = EventJournal(conversation_id="conv_parallel_001", logs_dir=logs_dir)
    summary_mgr = GlobalSummaryManager(db_path=Path(tmp_dir) / "summaries.db")
    boundary = PermissionBoundary()
    reg = ToolRegistry()

    yield {
        "dir": tmp_dir,
        "store": store,
        "journal": journal,
        "summary_mgr": summary_mgr,
        "boundary": boundary,
        "registry": reg
    }

    store.close()
    journal.close()
    shutil.rmtree(tmp_dir, ignore_errors=True)


def test_tool_definition_concurrency_metadata():
    """Verifies that ToolDefinition tracks read_only and automatically infers for L0."""
    # L0 Observe tool -> automatically read_only=True
    t0 = ToolDefinition(
        tool_id="test.observe",
        name="test.observe",
        description="Observe status",
        parameters={"type": "object"},
        authority_tier=PermissionTier.L0_OBSERVE,
        handler=lambda args: {"status": "ok"}
    )
    assert t0.read_only is True

    # L2 Mutating tool -> read_only=False by default
    t2 = ToolDefinition(
        tool_id="test.mutate",
        name="test.mutate",
        description="Mutate file",
        parameters={"type": "object"},
        authority_tier=PermissionTier.L2_EXECUTE_REVERSIBLE,
        handler=lambda args: {"status": "written"}
    )
    assert t2.read_only is False


def test_parallel_tool_execution_speedup(temp_agent_env):
    """Verifies that multiple independent read-only tool calls execute concurrently in parallel."""
    reg = temp_agent_env["registry"]
    store = temp_agent_env["store"]
    journal = temp_agent_env["journal"]
    summary_mgr = temp_agent_env["summary_mgr"]
    boundary = temp_agent_env["boundary"]

    # Register 3 simulated read-only tools that each sleep 0.4s
    def make_slow_handler(tag: str):
        def _handler(args):
            time.sleep(0.4)
            return {"tag": tag, "val": args.get("val", 0) * 2}
        return _handler

    for i in range(1, 4):
        reg.register(ToolDefinition(
            tool_id=f"tool_slow_{i}",
            name=f"tool_slow_{i}",
            description=f"Slow read tool {i}",
            parameters={"type": "object", "properties": {"val": {"type": "integer"}}},
            authority_tier=PermissionTier.L0_OBSERVE,
            read_only=True,
            handler=make_slow_handler(f"worker_{i}")
        ), permission_boundary=boundary)

    # Plan: In iteration 0, model yields 3 parallel tool calls.
    # In iteration 1, model finishes with final text.
    plan = [
        {
            "tool_calls": [
                {"call_id": "c1", "name": "tool_slow_1", "args": {"val": 10}},
                {"call_id": "c2", "name": "tool_slow_2", "args": {"val": 20}},
                {"call_id": "c3", "name": "tool_slow_3", "args": {"val": 30}},
            ]
        },
        {
            "text": "All 3 parallel computations completed successfully."
        }
    ]
    mock_provider = MockParallelProvider(plan)

    executor = AgentExecutor(
        trajectory_store=store,
        event_journal=journal,
        summary_manager=summary_mgr,
        permission_boundary=boundary,
        tool_registry=reg,
        model_provider=mock_provider
    )

    start_time = time.time()
    events = list(executor.execute_turn("Compute parallel tools"))
    duration = time.time() - start_time

    # If executed serially: 3 * 0.4s = 1.2s minimum.
    # When executed concurrently in parallel: ~0.4s to 0.7s total!
    assert duration < 1.0, f"Expected parallel speedup (<1.0s), took {duration:.3f}s"

    # Verify deterministic trajectory ordering:
    # Steps:
    # 0: USER_INPUT
    # 1: TOOL_CALL (c1)
    # 2: TOOL_CALL (c2)
    # 3: TOOL_CALL (c3)
    # 4: TOOL_RESULT (c1)
    # 5: TOOL_RESULT (c2)
    # 6: TOOL_RESULT (c3)
    # 7: AGENT_TURN
    steps = store.list_steps()
    assert len(steps) == 8

    assert steps[1].step_type == StepType.TOOL_CALL
    assert steps[1].step_payload["call_id"] == "c1"
    assert steps[2].step_type == StepType.TOOL_CALL
    assert steps[2].step_payload["call_id"] == "c2"
    assert steps[3].step_type == StepType.TOOL_CALL
    assert steps[3].step_payload["call_id"] == "c3"

    assert steps[4].step_type == StepType.TOOL_RESULT
    assert steps[4].step_payload["tool_call_id"] == "c1"
    assert steps[5].step_type == StepType.TOOL_RESULT
    assert steps[5].step_payload["tool_call_id"] == "c2"
    assert steps[6].step_type == StepType.TOOL_RESULT
    assert steps[6].step_payload["tool_call_id"] == "c3"

    assert steps[7].step_type == StepType.AGENT_TURN


def test_serial_barrier_with_mutating_tool(temp_agent_env):
    """Verifies that mutating tools act as serial barriers between parallel read batches."""
    reg = temp_agent_env["registry"]
    store = temp_agent_env["store"]
    journal = temp_agent_env["journal"]
    summary_mgr = temp_agent_env["summary_mgr"]
    boundary = temp_agent_env["boundary"]

    execution_order = []

    def make_tracker(name: str, delay: float):
        def _h(args):
            time.sleep(delay)
            execution_order.append(name)
            return {"name": name}
        return _h

    # Two read tools and one mutating tool
    reg.register(ToolDefinition(
        tool_id="read_a",
        name="read_a",
        description="read a",
        parameters={"type": "object"},
        authority_tier=PermissionTier.L0_OBSERVE,
        read_only=True,
        handler=make_tracker("read_a", 0.05)
    ), permission_boundary=boundary)

    reg.register(ToolDefinition(
        tool_id="write_b",
        name="write_b",
        description="write b",
        parameters={"type": "object"},
        authority_tier=PermissionTier.L2_EXECUTE_REVERSIBLE,
        read_only=False,
        handler=make_tracker("write_b", 0.05)
    ), permission_boundary=boundary)

    reg.register(ToolDefinition(
        tool_id="read_c",
        name="read_c",
        description="read c",
        parameters={"type": "object"},
        authority_tier=PermissionTier.L0_OBSERVE,
        read_only=True,
        handler=make_tracker("read_c", 0.05)
    ), permission_boundary=boundary)

    plan = [
        {
            "tool_calls": [
                {"call_id": "c1", "name": "read_a", "args": {}},
                {"call_id": "c2", "name": "write_b", "args": {}},
                {"call_id": "c3", "name": "read_c", "args": {}},
            ]
        },
        {"text": "Barrier test completed."}
    ]
    mock_provider = MockParallelProvider(plan)

    executor = AgentExecutor(
        trajectory_store=store,
        event_journal=journal,
        summary_manager=summary_mgr,
        permission_boundary=boundary,
        tool_registry=reg,
        model_provider=mock_provider
    )

    list(executor.execute_turn("Run with barrier"))
    assert execution_order == ["read_a", "write_b", "read_c"]


def test_parallel_error_isolation(temp_agent_env):
    """Verifies that an error in one concurrent tool does not abort other tools in the batch."""
    reg = temp_agent_env["registry"]
    store = temp_agent_env["store"]
    journal = temp_agent_env["journal"]
    summary_mgr = temp_agent_env["summary_mgr"]
    boundary = temp_agent_env["boundary"]

    reg.register(ToolDefinition(
        tool_id="good_tool_1",
        name="good_tool_1",
        description="good",
        parameters={"type": "object"},
        authority_tier=PermissionTier.L0_OBSERVE,
        read_only=True,
        handler=lambda args: {"result": "success 1"}
    ), permission_boundary=boundary)

    def _failing_handler(args):
        raise ValueError("Simulated network outage")

    reg.register(ToolDefinition(
        tool_id="bad_tool",
        name="bad_tool",
        description="bad",
        parameters={"type": "object"},
        authority_tier=PermissionTier.L0_OBSERVE,
        read_only=True,
        handler=_failing_handler
    ), permission_boundary=boundary)

    reg.register(ToolDefinition(
        tool_id="good_tool_2",
        name="good_tool_2",
        description="good",
        parameters={"type": "object"},
        authority_tier=PermissionTier.L0_OBSERVE,
        read_only=True,
        handler=lambda args: {"result": "success 2"}
    ), permission_boundary=boundary)

    plan = [
        {
            "tool_calls": [
                {"call_id": "c1", "name": "good_tool_1", "args": {}},
                {"call_id": "c2", "name": "bad_tool", "args": {}},
                {"call_id": "c3", "name": "good_tool_2", "args": {}},
            ]
        },
        {"text": "Handled errors."}
    ]
    mock_provider = MockParallelProvider(plan)

    executor = AgentExecutor(
        trajectory_store=store,
        event_journal=journal,
        summary_manager=summary_mgr,
        permission_boundary=boundary,
        tool_registry=reg,
        model_provider=mock_provider
    )

    events = list(executor.execute_turn("Run error isolation"))
    steps = store.list_steps()

    # Steps:
    # 4: good_tool_1 -> DONE
    # 5: bad_tool -> ERROR
    # 6: good_tool_2 -> DONE
    assert steps[4].status == StepStatus.DONE
    assert steps[5].status == StepStatus.ERROR
    assert "Simulated network outage" in str(steps[5].step_payload["content"])
    assert steps[6].status == StepStatus.DONE
