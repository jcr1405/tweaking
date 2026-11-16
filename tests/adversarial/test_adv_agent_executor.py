"""Adversarial Verification Suite: Agent Executor & Model Provider Resiliency
Verifies turn loop recovery under provider failure, cancellation, infinite tool loops, and permission blocks.
"""

import os
import sys
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Dict, Any, List, Iterator, Optional

from runtime.agent_executor import AgentExecutor, CancellationToken, AgentExecutionEvent
from runtime.trajectory import TrajectoryStore, Step, StepType, StepStatus
from runtime.journal import EventJournal
from runtime.summaries import GlobalSummaryManager
from runtime.permissions import PermissionBoundary, PermissionTier, ActionPolicy
from runtime.tools.registry import ToolRegistry, ToolDefinition
from runtime.providers.base import IModelProvider, ModelStreamChunk
from runtime.artifacts.store import ArtifactStore
from runtime.conversation import ConversationMetadata


class MockFailingProvider(IModelProvider):
    """Fault-injecting model provider."""

    def __init__(self, failure_mode: str = "network_error"):
        self.failure_mode = failure_mode
        self._provider_name = "mock_failing"
        self._model_id = "mock-v1"

    @property
    def provider_name(self) -> str:
        return self._provider_name

    @property
    def model_id(self) -> str:
        return self._model_id

    def validate_configuration(self) -> tuple[bool, str]:
        return True, "Mock ready"

    def generate_stream(
        self,
        system_prompt: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs
    ) -> Iterator[ModelStreamChunk]:
        if self.failure_mode == "network_error":
            yield ModelStreamChunk(text_delta="Thinking...")
            raise ConnectionResetError("Remote host forcibly closed an existing connection (WSAECONNRESET)")
        elif self.failure_mode == "infinite_tool_loop":
            # Always request the same tool
            yield ModelStreamChunk(
                tool_calls=[{"call_id": "call_loop", "name": "mock.loop_tool", "args": {}}]
            )
        elif self.failure_mode == "l3_unauthorized_tool":
            # Requests an L3 tool without consent
            yield ModelStreamChunk(
                tool_calls=[{"call_id": "call_l3", "name": "system.format_disk", "args": {}}]
            )


class TestAdvAgentExecutor(unittest.TestCase):
    """Adversarial test cases for AgentExecutor turn loop."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="adv_agent_")
        self.base_dir = Path(self.temp_dir)
        self.conv_id = "adv-agent-turn-001"

        self.store = TrajectoryStore(conversation_id=self.conv_id, db_dir=self.base_dir / "trajectories")
        self.store.save_metadata(ConversationMetadata(conversation_id=self.conv_id, title="Agent Test"))
        self.journal = EventJournal(conversation_id=self.conv_id, logs_dir=self.base_dir / "logs")
        self.summary_mgr = GlobalSummaryManager(db_path=self.base_dir / "summaries.db")
        self.boundary = PermissionBoundary()
        self.registry = ToolRegistry()
        self.artifact_store = ArtifactStore(root_dir=self.base_dir / "artifacts")

    def tearDown(self):
        self.store.close()
        self.journal.close()
        self.summary_mgr.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_provider_network_crash_yields_turn_failed(self):
        """Verifies that when a model stream drops with a connection error, the executor emits TURN_FAILED cleanly."""
        provider = MockFailingProvider(failure_mode="network_error")
        executor = AgentExecutor(
            trajectory_store=self.store,
            event_journal=self.journal,
            summary_manager=self.summary_mgr,
            permission_boundary=self.boundary,
            tool_registry=self.registry,
            model_provider=provider,
            artifact_store=self.artifact_store
        )

        events = list(executor.execute_turn("hello agent"))
        event_types = [e.event_type for e in events]
        self.assertIn("TURN_FAILED", event_types)
        failed_event = next(e for e in events if e.event_type == "TURN_FAILED")
        self.assertIn("WSAECONNRESET", failed_event.payload.get("error", ""))

    def test_infinite_tool_recursion_is_capped(self):
        """Verifies that infinite model tool loops are hard-capped at max_tool_iterations."""
        provider = MockFailingProvider(failure_mode="infinite_tool_loop")
        self.registry.register(
            ToolDefinition(
                tool_id="mock.loop_tool",
                name="loop_tool",
                description="Harmless loop test",
                parameters={},
                authority_tier=PermissionTier.L0_OBSERVE,
                handler=lambda args: {"counter": 1}
            ),
            permission_boundary=self.boundary
        )

        executor = AgentExecutor(
            trajectory_store=self.store,
            event_journal=self.journal,
            summary_manager=self.summary_mgr,
            permission_boundary=self.boundary,
            tool_registry=self.registry,
            model_provider=provider,
            artifact_store=self.artifact_store
        )

        events = list(executor.execute_turn("trigger loop", max_tool_iterations=3))
        event_types = [e.event_type for e in events]
        self.assertIn("TURN_FAILED", event_types)
        failed_event = next(e for e in events if e.event_type == "TURN_FAILED")
        self.assertIn("Maximum tool execution iterations exceeded", failed_event.payload.get("error", ""))

    def test_l3_unauthorized_tool_request_fails_and_records_step(self):
        """Verifies that when model calls an unauthorized L3 tool, TOOL_FAILED is emitted and step is recorded."""
        provider = MockFailingProvider(failure_mode="l3_unauthorized_tool")
        self.registry.register(
            ToolDefinition(
                tool_id="system.format_disk",
                name="format_disk",
                description="Destructive action",
                parameters={},
                authority_tier=PermissionTier.L3_PROTECTED,
                handler=lambda args: {"wiped": True}
            ),
            permission_boundary=self.boundary
        )

        executor = AgentExecutor(
            trajectory_store=self.store,
            event_journal=self.journal,
            summary_manager=self.summary_mgr,
            permission_boundary=self.boundary,
            tool_registry=self.registry,
            model_provider=provider,
            artifact_store=self.artifact_store
        )

        events = list(executor.execute_turn("wipe disk", max_tool_iterations=1))
        event_types = [e.event_type for e in events]
        self.assertIn("TOOL_FAILED", event_types)
        tool_failed_event = next(e for e in events if e.event_type == "TOOL_FAILED")
        self.assertIn("requires explicit user consent", str(tool_failed_event.payload.get("error", "")))


if __name__ == "__main__":
    unittest.main()
