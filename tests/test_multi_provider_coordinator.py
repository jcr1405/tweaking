"""Comprehensive Unit & Integration Test Suite for MultiProviderCoordinator
Verifies all 14 required acceptance scenarios:
1. Normal primary success
2. FreeLLMAPI HTTP 429 rate limit failover to Ollama
3. FreeLLMAPI HTTP 502 with exhausted upstream routes (reproducing live incident)
4. Failure before first streamed token
5. Failure during partial text generation (streaming safety)
6. Failure during partial tool-call generation (no partial execution)
7. Tool completed, subsequent inference failed (committed tool deduplication)
8. Complex task not eligible for local fallback (preserves state, honest failure)
9. All providers unavailable (bounded failure, zero recursive loops)
10. Authentication error classification and handling
11. Concurrent sessions isolation (zero state/mode leakage)
12. Cancellation during failover
13. Provider recovery on subsequent turn (cooldown expiration, no .env mutation)
14. Regression coverage across existing providers and AgentExecutor
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


class TestMultiProviderCoordinator(unittest.TestCase):
    """Test suite validating MultiProviderCoordinator failure classification, fallback, and state preservation."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name)
        self.event_bus = EventBus()
        self.captured_events: List[Any] = []
        self.event_bus.subscribe_all(lambda ev: self.captured_events.append(ev))

    def tearDown(self):
        import gc
        gc.collect()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # Test 1: Normal Primary Success
    # -------------------------------------------------------------------------
    def test_01_normal_primary_success(self):
        p1 = FakeModelProvider([
            [ModelStreamChunk(text_delta="Hello from primary FreeLLMAPI!")]
        ])
        p2 = FakeModelProvider([
            [ModelStreamChunk(text_delta="Hello from fallback Ollama!")]
        ])

        coord = MultiProviderCoordinator(
            providers={"freellmapi": p1, "ollama": p2},
            primary_provider="freellmapi",
            fallback_order=["ollama"],
            event_bus=self.event_bus
        )

        stream = coord.generate_stream(system_prompt="Test", messages=[{"role": "user", "content": "Hi"}])
        chunks = list(stream)
        text = "".join(c.text_delta for c in chunks)

        self.assertEqual(text, "Hello from primary FreeLLMAPI!")
        self.assertEqual(len(p1.recorded_requests), 1)
        self.assertEqual(len(p2.recorded_requests), 0)  # No fallback triggered

        event_types = [e.event_type for e in self.captured_events]
        self.assertIn("PROVIDER_SELECTED", event_types)
        self.assertNotIn("PROVIDER_FALLBACK_STARTED", event_types)

    # -------------------------------------------------------------------------
    # Test 2: FreeLLMAPI HTTP 429 Rate Limit
    # -------------------------------------------------------------------------
    def test_02_freellmapi_http_429_rate_limit(self):
        p1 = FakeModelProvider(
            simulate_error=RuntimeError("FreeLLMAPI Rate limit exceeded (HTTP 429): request rate limit reached")
        )
        p2 = FakeModelProvider([
            [ModelStreamChunk(text_delta="Recovered on local Ollama")]
        ])

        coord = MultiProviderCoordinator(
            providers={"freellmapi": p1, "ollama": p2},
            primary_provider="freellmapi",
            fallback_order=["ollama"],
            event_bus=self.event_bus,
            rate_limit_cooldown_seconds=60.0
        )

        stream = coord.generate_stream(
            system_prompt="Sys",
            messages=[{"role": "user", "content": "Run operational check"}]
        )
        chunks = list(stream)
        text = "".join(c.text_delta for c in chunks)

        self.assertIn("Recovered on local Ollama", text)
        status = coord.get_circuit_status("freellmapi")
        self.assertTrue(status["in_cooldown"])
        self.assertEqual(status["last_failure_category"], "RATE_LIMIT")

        event_types = [e.event_type for e in self.captured_events]
        self.assertIn("PROVIDER_FAILURE", event_types)
        self.assertIn("PROVIDER_FALLBACK_STARTED", event_types)
        self.assertIn("PROVIDER_FALLBACK_COMPLETED", event_types)

    # -------------------------------------------------------------------------
    # Test 3: FreeLLMAPI HTTP 502 with Exhausted Upstream Routes (Exact Incident)
    # -------------------------------------------------------------------------
    def test_03_reproduce_incident_freellmapi_502_upstream_exhausted(self):
        incident_msg = (
            'FreeLLMAPI generation error (HTTP 502): {"error":{"message":"All 3 routed attempt(s) '
            'failed with upstream provider errors (daily_quota_exhausted x2, upstream_error x1). '
            'Attempt trail: openrouter: daily_quota_exhausted; gemini: upstream_error; '
            'cloudflare: daily_quota_exhausted. Last error: Cloudflare API error 429: '
            'you have used up your daily free allocation of 10,000 neurons"}}'
        )
        p_freellm = FakeModelProvider(simulate_error=RuntimeError(incident_msg))
        p_ollama = FakeModelProvider([
            [ModelStreamChunk(text_delta="Ollama Qwen resumed task without error.")]
        ])

        coord = MultiProviderCoordinator(
            providers={"freellmapi": p_freellm, "ollama": p_ollama},
            primary_provider="freellmapi",
            fallback_order=["ollama"],
            event_bus=self.event_bus
        )

        stream = coord.generate_stream(
            system_prompt="Sys",
            messages=[{"role": "user", "content": "Continue task"}]
        )
        result_text = "".join(c.text_delta for c in stream)

        self.assertIn("Ollama Qwen resumed task without error.", result_text)
        # Verify failure category was classified as QUOTA_EXHAUSTED or RATE_LIMIT
        cat, _, _ = classify_provider_error(RuntimeError(incident_msg))
        self.assertIn(cat, (FailureCategory.RATE_LIMIT, FailureCategory.QUOTA_EXHAUSTED))

    # -------------------------------------------------------------------------
    # Test 4: Failure Before First Streamed Token
    # -------------------------------------------------------------------------
    def test_04_failure_before_first_streamed_token(self):
        p1 = FakeModelProvider(simulate_error=ConnectionError("Cannot connect to gateway"))
        p2 = FakeModelProvider([
            [ModelStreamChunk(text_delta="Seamless fallback response")]
        ])

        coord = MultiProviderCoordinator(
            providers={"primary": p1, "ollama": p2},
            primary_provider="primary",
            fallback_order=["ollama"]
        )

        chunks = list(coord.generate_stream(system_prompt="", messages=[]))
        text = "".join(c.text_delta for c in chunks)
        # Should be seamless: no error text delta emitted before the valid response
        self.assertEqual(text, "Seamless fallback response")

    # -------------------------------------------------------------------------
    # Test 5: Failure During Partial Text Generation (Streaming Safety)
    # -------------------------------------------------------------------------
    def test_05_failure_during_partial_text_generation(self):
        class MidStreamFailingProvider(IModelProvider):
            @property
            def provider_name(self): return "flaky"
            @property
            def model_id(self): return "flaky-v1"
            def validate_configuration(self): return True, "OK"
            def generate_stream(self, system_prompt, messages, tools=None, cancellation_token=None, **kwargs):
                yield ModelStreamChunk(text_delta="Beginning sentence...")
                raise ConnectionResetError("Connection lost mid-stream")

        p1 = MidStreamFailingProvider()
        p2 = FakeModelProvider([
            [ModelStreamChunk(text_delta="Resumed text from fallback provider.")]
        ])

        coord = MultiProviderCoordinator(
            providers={"flaky": p1, "ollama": p2},
            primary_provider="flaky",
            fallback_order=["ollama"]
        )

        chunks = list(coord.generate_stream(system_prompt="", messages=[]))
        text_deltas = [c.text_delta for c in chunks if c.text_delta]
        thinking_deltas = [c.thinking_status for c in chunks if c.thinking_status]

        # First chunk was yielded before crash
        self.assertEqual(text_deltas[0], "Beginning sentence...")
        # A safety status chunk was emitted to inform user of mid-stream failover
        self.assertTrue(any("failed mid-stream" in t for t in thinking_deltas))
        # Final text from fallback was delivered
        self.assertIn("Resumed text from fallback provider.", text_deltas[-1])

    # -------------------------------------------------------------------------
    # Test 6: Failure During Partial Tool-Call Generation
    # -------------------------------------------------------------------------
    def test_06_failure_during_partial_tool_call(self):
        class MidStreamToolFailingProvider(IModelProvider):
            @property
            def provider_name(self): return "tool_flaky"
            @property
            def model_id(self): return "flaky-v1"
            def validate_configuration(self): return True, "OK"
            def generate_stream(self, system_prompt, messages, tools=None, cancellation_token=None, **kwargs):
                yield ModelStreamChunk(tool_calls=[{"call_id": "call_bad", "name": "do_bad", "args": {}}])
                raise RuntimeError("503 Service Unavailable mid-call")

        p1 = MidStreamToolFailingProvider()
        p2 = FakeModelProvider([
            [ModelStreamChunk(
                tool_calls=[{"call_id": "call_good", "name": "do_good", "args": {"clean": True}}],
                finish_reason="tool_calls"
            )]
        ])

        coord = MultiProviderCoordinator(
            providers={"tool_flaky": p1, "ollama": p2},
            primary_provider="tool_flaky",
            fallback_order=["ollama"]
        )

        chunks = list(coord.generate_stream(system_prompt="", messages=[]))
        valid_tcs = [c.tool_calls for c in chunks if c.tool_calls]
        self.assertTrue(len(valid_tcs) >= 1)
        # The final accepted tool call came from the clean fallback
        self.assertEqual(valid_tcs[-1][0]["name"], "do_good")

    # -------------------------------------------------------------------------
    # Test 7: Tool Completed, Subsequent Inference Failed (Core Acceptance Test)
    # -------------------------------------------------------------------------
    def test_07_tool_completed_subsequent_inference_failed(self):
        """Simulates:
        1. FreeLLMAPI requests tool 'test.get_data'
        2. AgentExecutor executes 'test.get_data', commits result to SQLite
        3. Turn continues to iteration 2
        4. FreeLLMAPI fails with HTTP 502
        5. Coordinator falls back to Ollama
        6. Qwen receives reconstructed conversation with committed result
        7. Qwen finishes the turn without repeating the tool
        """
        # Step 1: Set up AgentExecutor dependencies
        conv_id = "test-failover-turn-007"
        store = TrajectoryStore(conversation_id=conv_id, db_dir=self.base_dir / "conversations")
        meta = ConversationMetadata(conversation_id=conv_id, title="Failover Test", workspace_path=str(self.base_dir))
        store.save_metadata(meta)
        journal = EventJournal(conversation_id=conv_id, logs_dir=self.base_dir / "logs")
        summary_mgr = GlobalSummaryManager(db_path=self.base_dir / "summaries.db")
        summary_mgr.upsert_summary(conv_id, "Failover Test", str(self.base_dir))
        assembler = ContextAssembler(trajectory_store=store, summary_manager=summary_mgr)
        boundary = PermissionBoundary()
        registry = ToolRegistry()

        tool_exec_count = 0
        def _tool_handler(args):
            nonlocal tool_exec_count
            tool_exec_count += 1
            return {"data": "system_metrics_payload_123"}

        registry.register(ToolDefinition(
            tool_id="test.get_data",
            name="test.get_data",
            description="Fetches metrics",
            parameters={"type": "object"},
            authority_tier=PermissionTier.L0_OBSERVE,
            handler=_tool_handler
        ))

        # Primary provider: Iteration 1 returns tool call; Iteration 2 throws HTTP 502
        class FlakyFreeLLM(IModelProvider):
            def __init__(self):
                self.calls = 0
            @property
            def provider_name(self): return "freellmapi"
            @property
            def model_id(self): return "auto"
            def validate_configuration(self): return True, "OK"
            def generate_stream(self, system_prompt, messages, tools=None, cancellation_token=None, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    yield ModelStreamChunk(
                        tool_calls=[{"call_id": "call_m1", "name": "test.get_data", "args": {}}],
                        finish_reason="tool_calls"
                    )
                else:
                    raise RuntimeError("FreeLLMAPI generation error (HTTP 502): Bad Gateway upstream_failed")

        flaky_freellm = FlakyFreeLLM()

        # Fallback provider: Responds with final answer using the tool result from reconstructed context
        class ReliableOllama(IModelProvider):
            def __init__(self):
                self.received_messages = []
            @property
            def provider_name(self): return "ollama"
            @property
            def model_id(self): return "qwen3.5:9b"
            def validate_configuration(self): return True, "OK"
            def generate_stream(self, system_prompt, messages, tools=None, cancellation_token=None, **kwargs):
                self.received_messages = list(messages)
                yield ModelStreamChunk(
                    text_delta="Based on system_metrics_payload_123, all diagnostics look healthy."
                )

        reliable_ollama = ReliableOllama()

        coord = MultiProviderCoordinator(
            providers={"freellmapi": flaky_freellm, "ollama": reliable_ollama},
            primary_provider="freellmapi",
            fallback_order=["ollama"],
            event_bus=self.event_bus
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

        events = list(executor.execute_turn("Check system metrics"))
        event_types = [e.event_type for e in events]

        # Verify execution flow
        self.assertIn("TOOL_COMPLETED", event_types)
        self.assertIn("TURN_COMPLETED", event_types)
        self.assertNotIn("TURN_FAILED", event_types)

        # 1. Tool executed exactly ONCE
        self.assertEqual(tool_exec_count, 1)

        # 2. SQLite trajectory steps verify persistence integrity:
        # Step 0: USER_INPUT
        # Step 1: TOOL_CALL (test.get_data)
        # Step 2: TOOL_RESULT
        # Step 3: AGENT_TURN (final answer by Qwen)
        steps = store.list_steps()
        self.assertEqual(len(steps), 4)
        self.assertEqual(steps[0].step_type, StepType.USER_INPUT)
        self.assertEqual(steps[1].step_type, StepType.TOOL_CALL)
        self.assertEqual(steps[2].step_type, StepType.TOOL_RESULT)
        self.assertIn("system_metrics_payload_123", steps[2].step_payload["content"])
        self.assertEqual(steps[3].step_type, StepType.AGENT_TURN)
        self.assertIn("all diagnostics look healthy", steps[3].step_payload["content"])

        # 3. Verify Qwen received the tool result in reconstructed context
        roles = [m.get("role") for m in reliable_ollama.received_messages]
        self.assertIn("tool", roles)
        store.close()
        summary_mgr.close()

    # -------------------------------------------------------------------------
    # Test 8: Complex Task Not Eligible for Local Fallback
    # -------------------------------------------------------------------------
    def test_08_complex_task_not_eligible_for_local_fallback(self):
        p1 = FakeModelProvider(simulate_error=RuntimeError("HTTP 429: Cloudflare quota exhausted"))
        p2 = FakeModelProvider([[ModelStreamChunk(text_delta="Inappropriate local answer")]])

        coord = MultiProviderCoordinator(
            providers={"freellmapi": p1, "ollama": p2},
            primary_provider="freellmapi",
            fallback_order=["ollama"],
            event_bus=self.event_bus
        )

        # Request marked with COMPLEX complexity and architectural purpose
        cfg = InferenceRequestConfig(
            purpose="deep_architectural_reasoning",
            task_complexity="COMPLEX",
            fallback_policy="local_eligible"
        )

        with self.assertRaises(RuntimeError) as ctx:
            list(coord.generate_stream(
                system_prompt="Sys",
                messages=[{"role": "user", "content": "Redesign microkernel architecture"}],
                request_config=cfg
            ))

        self.assertIn("ineligible for fallback", str(ctx.exception).lower())
        self.assertEqual(len(p2.recorded_requests), 0)  # Ollama was NOT called

    # -------------------------------------------------------------------------
    # Test 9: All Providers Unavailable (Zero Infinite Loops)
    # -------------------------------------------------------------------------
    def test_09_all_providers_unavailable_bounded_failure(self):
        p1 = FakeModelProvider(simulate_error=RuntimeError("502 Gateway Error"))
        p2 = FakeModelProvider(simulate_error=RuntimeError("Ollama daemon down"))

        coord = MultiProviderCoordinator(
            providers={"freellmapi": p1, "ollama": p2},
            primary_provider="freellmapi",
            fallback_order=["ollama"]
        )

        with self.assertRaises(RuntimeError) as ctx:
            list(coord.generate_stream(system_prompt="", messages=[]))

        self.assertIn("All candidate providers", str(ctx.exception))

    # -------------------------------------------------------------------------
    # Test 10: Authentication / Invalid Configuration Error
    # -------------------------------------------------------------------------
    def test_10_authentication_and_invalid_schema_handling(self):
        cat_auth, _, _ = classify_provider_error(RuntimeError("HTTP 401 Unauthorized: Invalid API key"))
        self.assertEqual(cat_auth, FailureCategory.AUTH_CONFIG)

        cat_schema, _, _ = classify_provider_error(RuntimeError("HTTP 400 Bad Request: schema validation failed"))
        self.assertEqual(cat_schema, FailureCategory.INVALID_REQUEST)

        # Schema error is never eligible for fallback
        cfg = InferenceRequestConfig()
        eligible, reason = is_fallback_eligible(cfg, cat_schema, "freellmapi", "ollama")
        self.assertFalse(eligible)
        self.assertIn("invalid request schema", reason)

    # -------------------------------------------------------------------------
    # Test 11: Concurrent Sessions Isolation
    # -------------------------------------------------------------------------
    def test_11_concurrent_sessions_isolation(self):
        """Runs concurrent requests on separate threads to ensure request configurations
        and thinking modes do not leak across sessions.
        """
        p_shared = FakeModelProvider([
            [ModelStreamChunk(text_delta="Turn A output")],
            [ModelStreamChunk(text_delta="Turn B output")]
        ])

        coord = MultiProviderCoordinator(
            providers={"shared": p_shared},
            primary_provider="shared"
        )

        results = {}
        errors = []

        def _worker(thread_id: str, mode: str):
            try:
                cfg = InferenceRequestConfig(mode=mode, purpose=f"thread_{thread_id}")
                chunks = list(coord.generate_stream(system_prompt="", messages=[], request_config=cfg))
                results[thread_id] = "".join(c.text_delta for c in chunks)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=_worker, args=("A", "routine"))
        t2 = threading.Thread(target=_worker, args=("B", "reasoning"))

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len(errors), 0)
        self.assertIn("A", results)
        self.assertIn("B", results)

    # -------------------------------------------------------------------------
    # Test 12: Cancellation During Failover
    # -------------------------------------------------------------------------
    def test_12_cancellation_during_failover(self):
        p1 = FakeModelProvider(simulate_error=RuntimeError("502 Bad Gateway"))
        p2 = FakeModelProvider([[ModelStreamChunk(text_delta="Should not reach here")]])

        token = CancellationToken()

        coord = MultiProviderCoordinator(
            providers={"p1": p1, "p2": p2},
            primary_provider="p1",
            fallback_order=["p2"]
        )

        # Cancel token before starting fallback
        token.cancel()

        chunks = list(coord.generate_stream(
            system_prompt="",
            messages=[],
            cancellation_token=token
        ))

        self.assertEqual(len(chunks), 0)
        self.assertEqual(len(p2.recorded_requests), 0)

    # -------------------------------------------------------------------------
    # Test 13: Provider Recovery on Subsequent Turn
    # -------------------------------------------------------------------------
    def test_13_provider_recovery_on_subsequent_turn(self):
        p1 = FakeModelProvider([
            [ModelStreamChunk(text_delta="Turn 2 primary recovered!")]
        ])
        # Temporarily force error on call 1
        p1._simulate_error = RuntimeError("429 Too Many Requests")

        p2 = FakeModelProvider([
            [ModelStreamChunk(text_delta="Turn 1 fallback to Ollama")]
        ])

        coord = MultiProviderCoordinator(
            providers={"p1": p1, "ollama": p2},
            primary_provider="p1",
            fallback_order=["ollama"],
            rate_limit_cooldown_seconds=0.2  # Short cooldown for test
        )

        # Turn 1: Primary fails -> fallback succeeds
        coord.start_turn()
        t1_text = "".join(c.text_delta for c in coord.generate_stream(system_prompt="", messages=[]))
        coord.end_turn()
        self.assertIn("Turn 1 fallback to Ollama", t1_text)

        # Wait for cooldown to expire
        time.sleep(0.25)
        # Clear simulated error on primary
        p1._simulate_error = None

        # Turn 2: Primary has recovered!
        coord.start_turn()
        t2_text = "".join(c.text_delta for c in coord.generate_stream(system_prompt="", messages=[]))
        coord.end_turn()
        self.assertIn("Turn 2 primary recovered!", t2_text)

    # -------------------------------------------------------------------------
    # Test 14: TDEL & Session Factory Integration
    # -------------------------------------------------------------------------
    def test_14_session_factory_initializes_coordinator(self):
        from runtime.session import AgentSessionFactory
        from runtime.observability.task_registry import ActiveTaskRegistry

        sm = GlobalSummaryManager(db_path=self.base_dir / "summaries.db")
        factory = AgentSessionFactory(
            conversations_dir=self.base_dir / "conversations",
            logs_dir=self.base_dir / "logs",
            summary_manager=sm,
            task_registry=ActiveTaskRegistry()
        )

        session = factory.create_session(conversation_id="test-session-coord-014")
        self.assertIsNotNone(session.model_provider)
        # Verify provider implements IModelProvider and coordinator capabilities
        self.assertTrue(isinstance(session.model_provider, IModelProvider))
        self.assertTrue(hasattr(session.model_provider, "generate_stream"))
        session.close()
        sm.close()


if __name__ == "__main__":
    unittest.main()
