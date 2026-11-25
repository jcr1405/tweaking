"""Presence Phase 1 Final Acceptance Audit Test Suite
Forensic read-only verification covering the 5 acceptance areas:
1. Shared coordinator concurrency (Session A fails to Ollama, Session B stays on FreeLLMAPI, Session C runs routine Ollama)
2. Quota exhaustion vs burst rate-limit vs Retry-After cooldown semantics
3. Streaming partial-text reset without corrupted text merging
4. Actual live Ollama fallback with real local Qwen 3.5 9B
5. TDEL, workflow, and subagent provider consistency
"""

import time
import json
import unittest
import tempfile
import threading
from pathlib import Path
from typing import List, Dict, Any, Optional

from runtime.providers.base import IModelProvider, ModelStreamChunk
from runtime.providers.fake import FakeModelProvider
from runtime.providers.ollama import OllamaModelProvider
from runtime.providers.coordinator import (
    MultiProviderCoordinator,
    InferenceRequestConfig,
    FailureCategory,
    classify_provider_error,
    is_fallback_eligible,
)
from runtime.events import EventBus
from runtime.conversation import ConversationMetadata
from runtime.trajectory import TrajectoryStore, Step, StepType, StepStatus
from runtime.journal import EventJournal
from runtime.summaries import GlobalSummaryManager
from runtime.reconstruction import ContextAssembler
from runtime.permissions import PermissionBoundary, PermissionTier
from runtime.tools.registry import ToolDefinition, ToolRegistry
from runtime.agent_executor import AgentExecutor, CancellationToken
from runtime.session import AgentSessionFactory
from runtime.observability.task_registry import ActiveTaskRegistry


class TestPhase1FinalAcceptance(unittest.TestCase):
    """Forensic verification for all 5 Phase 1 acceptance areas."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name)
        self.event_bus = EventBus()
        self.events_log = []
        self.event_bus.subscribe_all(lambda ev: self.events_log.append(ev))

    def tearDown(self):
        import gc
        gc.collect()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    # =========================================================================
    # Area 1: Shared Coordinator Concurrency
    # =========================================================================
    def test_01_shared_coordinator_concurrency_isolation(self):
        """Verifies that a SINGLE MultiProviderCoordinator instance shared across
        multiple concurrent AgentSessions retains strict conversation-isolated state:
        - Session A experiences FreeLLMAPI failure and switches to Ollama.
        - Session B continues using FreeLLMAPI.
        - Session C runs a separate Ollama routine request.
        Zero cross-session active provider or thinking-mode leakage.
        """
        # Session A: FreeLLMAPI fails, Ollama succeeds
        p_freellm_A = FakeModelProvider(simulate_error=RuntimeError("502 Bad Gateway"))
        # Session B: FreeLLMAPI succeeds
        p_freellm_B = FakeModelProvider([[ModelStreamChunk(text_delta="Session B response from FreeLLMAPI")]])
        # Fallback / Local Ollama provider
        p_ollama = FakeModelProvider([
            [ModelStreamChunk(text_delta="Session A fallback from Ollama")],
            [ModelStreamChunk(text_delta="Session C direct from Ollama")]
        ])

        # Composite multi-session mock for FreeLLMAPI that routes by conversation_id in request_config
        class RouterFreeLLM(IModelProvider):
            @property
            def provider_name(self): return "freellmapi"
            @property
            def model_id(self): return "auto"
            def validate_configuration(self): return True, "OK"
            def generate_stream(self, system_prompt, messages, tools=None, cancellation_token=None, request_config=None, **kwargs):
                cid = request_config.conversation_id if request_config else ""
                if cid == "conv-A":
                    raise RuntimeError("FreeLLMAPI HTTP 502: Bad Gateway upstream_failed")
                else:
                    yield ModelStreamChunk(text_delta="Session B response from FreeLLMAPI")

        shared_coord = MultiProviderCoordinator(
            providers={"freellmapi": RouterFreeLLM(), "ollama": p_ollama},
            primary_provider="freellmapi",
            fallback_order=["ollama"],
            event_bus=self.event_bus,
            rate_limit_cooldown_seconds=0.0,
            upstream_cooldown_seconds=0.0
        )

        results = {}
        active_providers = {}

        def _run_session_A():
            shared_coord.start_turn(conversation_id="conv-A")
            cfg = InferenceRequestConfig(conversation_id="conv-A")
            chunks = list(shared_coord.generate_stream(system_prompt="", messages=[], request_config=cfg))
            results["conv-A"] = "".join(c.text_delta for c in chunks)
            active_providers["conv-A"] = shared_coord.get_active_provider_for_conversation("conv-A")
            shared_coord.end_turn(conversation_id="conv-A")

        def _run_session_B():
            shared_coord.start_turn(conversation_id="conv-B")
            cfg = InferenceRequestConfig(conversation_id="conv-B")
            chunks = list(shared_coord.generate_stream(system_prompt="", messages=[], request_config=cfg))
            results["conv-B"] = "".join(c.text_delta for c in chunks)
            active_providers["conv-B"] = shared_coord.get_active_provider_for_conversation("conv-B")
            shared_coord.end_turn(conversation_id="conv-B")

        def _run_session_C():
            shared_coord.start_turn(conversation_id="conv-C")
            cfg = InferenceRequestConfig(provider="ollama", mode="routine", conversation_id="conv-C")
            chunks = list(shared_coord.generate_stream(system_prompt="", messages=[], request_config=cfg))
            results["conv-C"] = "".join(c.text_delta for c in chunks)
            active_providers["conv-C"] = shared_coord.get_active_provider_for_conversation("conv-C")
            shared_coord.end_turn(conversation_id="conv-C")

        # Run concurrently
        threads = [
            threading.Thread(target=_run_session_A),
            threading.Thread(target=_run_session_B),
            threading.Thread(target=_run_session_C),
        ]
        for t in threads: t.start()
        for t in threads: t.join()

        # Assertions
        # 1. Session A fell back to Ollama
        self.assertIn("Session A fallback from Ollama", results["conv-A"])
        self.assertEqual(active_providers["conv-A"], "ollama")

        # 2. Session B used FreeLLMAPI directly without being polluted by Session A's failover
        self.assertIn("Session B response from FreeLLMAPI", results["conv-B"])
        self.assertEqual(active_providers["conv-B"], "freellmapi")

        # 3. Session C executed direct Ollama routine request cleanly
        self.assertIn("Session C direct from Ollama", results["conv-C"])

    # =========================================================================
    # Area 2: Quota Exhaustion and Cooldown Semantics
    # =========================================================================
    def test_02_quota_exhaustion_vs_rate_limit_semantics(self):
        """Verifies distinction between:
        - Confirmed daily quota exhaustion (cooldown calculated to next midnight UTC >= 3600s)
        - Temporary burst 429 rate limit (60s)
        - Explicit Retry-After header
        - Upstream 502/503/504 failures (15s)
        """
        # A. Confirmed daily quota exhaustion (Exact Cloudflare / FreeLLMAPI incident)
        incident_err = RuntimeError(
            'FreeLLMAPI generation error (HTTP 502): {"error":{"message":"All 3 routed attempts failed '
            'daily_quota_exhausted x2. Cloudflare API error 429: you have used up your daily free allocation of 10,000 neurons"}}'
        )
        cat_daily, reason_daily, retry_daily = classify_provider_error(incident_err)
        self.assertEqual(cat_daily, FailureCategory.QUOTA_EXHAUSTED)
        self.assertIsNotNone(retry_daily)
        self.assertGreaterEqual(retry_daily, 3600.0)  # At least 1 hour, bounded to midnight UTC

        # B. Temporary burst rate limit (429 without daily/monthly words)
        burst_err = RuntimeError("HTTP 429: Too Many Requests. Rate limit exceeded, please slow down.")
        cat_burst, reason_burst, retry_burst = classify_provider_error(burst_err)
        self.assertEqual(cat_burst, FailureCategory.RATE_LIMIT)
        self.assertIsNone(retry_burst)  # Will receive default rate_limit_cooldown (60s)

        # C. Explicit Retry-After header
        retry_hdr_err = RuntimeError("HTTP 429: Too Many Requests. Retry-After: 120")
        cat_hdr, reason_hdr, retry_hdr = classify_provider_error(retry_hdr_err)
        self.assertEqual(cat_hdr, FailureCategory.RATE_LIMIT)
        self.assertEqual(retry_hdr, 120.0)

        # D. Upstream availability failure (HTTP 503)
        upstream_err = RuntimeError("HTTP 503: Service Unavailable upstream_error")
        cat_up, reason_up, retry_up = classify_provider_error(upstream_err)
        self.assertEqual(cat_up, FailureCategory.UPSTREAM_UNAVAILABLE)
        self.assertIsNone(retry_up)  # Will receive default upstream_cooldown (15s)

    # =========================================================================
    # Area 3: Streaming and TUI Recovery (No Text Merging, Clean Discard)
    # =========================================================================
    def test_03_streaming_partial_text_reset_no_merging(self):
        """Verifies that when a primary provider fails mid-stream after yielding partial text:
        1. Incomplete text from failed provider is NOT merged with fallback text
        2. Incomplete tool calls are discarded and never executed
        3. AgentExecutor emits TEXT_RESET event
        4. Final committed AGENT_TURN step contains ONLY the coherent fallback answer
        """
        conv_id = "test-stream-reset-003"
        store = TrajectoryStore(conversation_id=conv_id, db_dir=self.base_dir / "conversations")
        meta = ConversationMetadata(conversation_id=conv_id, title="Reset Test", workspace_path=str(self.base_dir))
        store.save_metadata(meta)
        journal = EventJournal(conversation_id=conv_id, logs_dir=self.base_dir / "logs")
        summary_mgr = GlobalSummaryManager(db_path=self.base_dir / "summaries.db")
        summary_mgr.upsert_summary(conv_id, "Reset Test", str(self.base_dir))
        assembler = ContextAssembler(trajectory_store=store, summary_manager=summary_mgr)
        boundary = PermissionBoundary()
        registry = ToolRegistry()

        class FlakyPartialStreamProvider(IModelProvider):
            @property
            def provider_name(self): return "flaky_cloud"
            @property
            def model_id(self): return "flaky-v1"
            def validate_configuration(self): return True, "OK"
            def generate_stream(self, system_prompt, messages, tools=None, cancellation_token=None, **kwargs):
                # Emit partial text delta
                yield ModelStreamChunk(text_delta="Incomplete corrupted preamble from failing cloud...")
                # Also emit a partial uncommitted tool call
                yield ModelStreamChunk(tool_calls=[{"call_id": "bad_tc", "name": "bad.action", "args": {}}])
                # Fail mid-stream
                raise ConnectionResetError("Connection lost mid-sentence")

        fallback_provider = FakeModelProvider([
            [ModelStreamChunk(text_delta="This is the complete and coherent answer generated cleanly by fallback.")]
        ])

        coord = MultiProviderCoordinator(
            providers={"flaky_cloud": FlakyPartialStreamProvider(), "ollama": fallback_provider},
            primary_provider="flaky_cloud",
            fallback_order=["ollama"],
            event_bus=self.event_bus,
            conversation_id=conv_id
        )

        executor = AgentExecutor(
            trajectory_store=store,
            event_journal=journal,
            summary_manager=summary_mgr,
            permission_boundary=boundary,
            tool_registry=registry,
            model_provider=coord,
            context_assembler=assembler
        )

        turn_events = list(executor.execute_turn("Tell me the answer"))
        event_types = [e.event_type for e in turn_events]

        # 1. TEXT_RESET event was emitted
        self.assertIn("TEXT_RESET", event_types)
        self.assertIn("TURN_COMPLETED", event_types)

        # 2. SQLite steps inspect: only USER_INPUT and AGENT_TURN exist
        steps = store.list_steps()
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0].step_type, StepType.USER_INPUT)
        self.assertEqual(steps[1].step_type, StepType.AGENT_TURN)

        final_content = steps[1].step_payload["content"]
        # Incomplete corrupted preamble MUST NOT be merged into final answer
        self.assertNotIn("Incomplete corrupted preamble", final_content)
        # Final answer contains only the clean fallback output
        self.assertEqual(final_content, "This is the complete and coherent answer generated cleanly by fallback.")

        # 3. No partial tool was executed or committed
        tc_steps = [s for s in steps if s.step_type == StepType.TOOL_CALL]
        self.assertEqual(len(tc_steps), 0)

        store.close()
        summary_mgr.close()

    # =========================================================================
    # Area 4: Actual Live Ollama Fallback with Real Qwen 3.5 9B
    # =========================================================================
    def test_04_live_ollama_turn_with_real_qwen(self):
        """Integration test with the REAL local Ollama daemon and qwen3.5:9b model.
        Simulates:
        1. Begin actual AgentExecutor turn.
        2. Receive valid tool call from primary provider.
        3. Execute through existing ToolRegistry.
        4. Commit result to SQLite.
        5. Simulate primary provider failure (HTTP 502) on follow-up inference.
        6. Coordinator activates REAL OllamaModelProvider (qwen3.5:9b, think=False).
        7. Reconstruct context using ContextAssembler.
        8. Generate final response using actual Qwen inference.
        9. Confirm completed tool was executed exactly once.
        """
        # Verify Ollama daemon readiness before test
        real_ollama = OllamaModelProvider(model_id="qwen3.5:9b", mode="routine", think=False)
        is_ready, msg = real_ollama.validate_configuration()
        if not is_ready:
            self.skipTest(f"Ollama daemon not reachable for live test: {msg}")

        conv_id = "test-live-ollama-failover-004"
        store = TrajectoryStore(conversation_id=conv_id, db_dir=self.base_dir / "conversations")
        meta = ConversationMetadata(conversation_id=conv_id, title="Live Qwen Test", workspace_path=str(self.base_dir))
        store.save_metadata(meta)
        journal = EventJournal(conversation_id=conv_id, logs_dir=self.base_dir / "logs")
        summary_mgr = GlobalSummaryManager(db_path=self.base_dir / "summaries.db")
        summary_mgr.upsert_summary(conv_id, "Live Qwen Test", str(self.base_dir))
        assembler = ContextAssembler(trajectory_store=store, summary_manager=summary_mgr)
        boundary = PermissionBoundary()
        registry = ToolRegistry()

        tool_execution_count = 0
        def _get_system_time(args):
            nonlocal tool_execution_count
            tool_execution_count += 1
            return {"current_system_status": "ONLINE", "disk_free_gb": 128}

        registry.register(ToolDefinition(
            tool_id="system.check_status",
            name="system.check_status",
            description="Checks status of system disks and resources",
            parameters={"type": "object"},
            authority_tier=PermissionTier.L0_OBSERVE,
            handler=_get_system_time
        ))

        # Flaky primary provider that requests the tool on iteration 1, then crashes with 502 on iteration 2
        class FlakyCloudGateway(IModelProvider):
            def __init__(self):
                self.call_count = 0
            @property
            def provider_name(self): return "freellmapi"
            @property
            def model_id(self): return "auto"
            def validate_configuration(self): return True, "OK"
            def generate_stream(self, system_prompt, messages, tools=None, cancellation_token=None, **kwargs):
                self.call_count += 1
                if self.call_count == 1:
                    yield ModelStreamChunk(
                        tool_calls=[{
                            "call_id": "call_stat_live",
                            "name": "system.check_status",
                            "args": {}
                        }],
                        finish_reason="tool_calls"
                    )
                else:
                    raise RuntimeError(
                        'FreeLLMAPI generation error (HTTP 502): {"error":{"message":"daily_quota_exhausted x2. '
                        'Cloudflare API error 429: allocation of 10,000 neurons exhausted"}}'
                    )

        coord = MultiProviderCoordinator(
            providers={"freellmapi": FlakyCloudGateway(), "ollama": real_ollama},
            primary_provider="freellmapi",
            fallback_order=["ollama"],
            event_bus=self.event_bus,
            conversation_id=conv_id
        )

        executor = AgentExecutor(
            trajectory_store=store,
            event_journal=journal,
            summary_manager=summary_mgr,
            permission_boundary=boundary,
            tool_registry=registry,
            model_provider=coord,
            context_assembler=assembler
        )

        start_t = time.time()
        events = list(executor.execute_turn("Check system status and disk space."))
        duration = time.time() - start_t

        event_types = [e.event_type for e in events]

        # 1. Verification of execution flow
        self.assertIn("TOOL_COMPLETED", event_types)
        self.assertIn("TURN_COMPLETED", event_types)
        self.assertNotIn("TURN_FAILED", event_types)

        # 2. Tool executed exactly ONCE
        self.assertEqual(tool_execution_count, 1)

        # 3. Check SQLite trajectory
        steps = store.list_steps()
        self.assertEqual(len(steps), 4)
        self.assertEqual(steps[0].step_type, StepType.USER_INPUT)
        self.assertEqual(steps[1].step_type, StepType.TOOL_CALL)
        self.assertEqual(steps[2].step_type, StepType.TOOL_RESULT)
        self.assertIn("disk_free_gb", steps[2].step_payload["content"])

        # 4. Final step: actual real Qwen response
        self.assertEqual(steps[3].step_type, StepType.AGENT_TURN)
        qwen_response = steps[3].step_payload["content"]
        self.assertTrue(len(qwen_response) > 10)
        # Check that Qwen understood the tool result and mentioned status or disk
        qwen_lower = qwen_response.lower()
        self.assertTrue(
            "online" in qwen_lower or "disk" in qwen_lower or "128" in qwen_lower or "status" in qwen_lower or "healthy" in qwen_lower,
            f"Expected Qwen to mention system status. Actual output: {qwen_response}"
        )

        store.close()
        summary_mgr.close()

    # =========================================================================
    # Area 5: TDEL and Workflow Provider Consistency
    # =========================================================================
    def test_05_tdel_workflow_subagents_provider_consistency(self):
        """Verifies that:
        1. AgentSessionFactory injects MultiProviderCoordinator into session.model_provider
        2. TaskPlanningService.planner receives the coordinator
        3. SubagentManager.session_factory passes the coordinator to child subagent sessions
        4. No subagent or TDEL component bypasses the coordinator with direct raw provider instances
        """
        from runtime.workflows.engine import WorkflowEngine
        from runtime.persistence import PersistenceManager
        from runtime.subagents.manager import SubagentManager

        sm = GlobalSummaryManager(db_path=self.base_dir / "summaries.db")
        persist = PersistenceManager(db_path=self.base_dir / "presence.db")
        wf_engine = WorkflowEngine(persistence=persist)
        task_reg = ActiveTaskRegistry()

        factory = AgentSessionFactory(
            conversations_dir=self.base_dir / "conversations",
            logs_dir=self.base_dir / "logs",
            summary_manager=sm,
            task_registry=task_reg,
            workflow_engine=wf_engine
        )
        sub_mgr = SubagentManager(session_factory=factory, task_registry=task_reg)
        factory.subagent_manager = sub_mgr

        session = factory.create_session(conversation_id="conv-consistency-005")

        # 1. Main session provider is MultiProviderCoordinator
        self.assertTrue(isinstance(session.model_provider, MultiProviderCoordinator))
        self.assertEqual(session.agent_executor.provider, session.model_provider)

        # 2. TaskPlanningService planner receives the coordinator
        self.assertIsNotNone(session.task_planning_service)
        self.assertEqual(session.task_planning_service.planner.model_provider, session.model_provider)

        # 3. Subagent session creation receives the coordinator
        if factory.subagent_manager:
            child_session = factory.subagent_manager.session_factory.create_session(conversation_id="child-subagent-001")
            self.assertTrue(isinstance(child_session.model_provider, MultiProviderCoordinator))
            child_session.close()

        session.close()
        sm.close()


if __name__ == "__main__":
    unittest.main()
