"""
Tests for Conversational vs Operational Intent Routing (Phase 1).
Verifies deterministic separation between conversational status/capability inquiries
and executable operational tasks, preventing false-positive TDEL preemption.
"""

import pytest
from unittest.mock import MagicMock
from runtime.task_planning.complexity import ComplexityClassifier
from runtime.task_planning.models import ComplexityCategory
from runtime.session import AgentSession
from runtime.agent_executor import AgentExecutionEvent


FAILED_CAPABILITY_AUDIT_PROMPT = """Give me a direct report of your current capabilities in this interactive session. IMPORTANT: This is a conversational status question, NOT a task. Do not invoke TDEL, create a DAG, create a workflow, delegate to subagents, or execute any tools. Do not modify files or interact with Windows. Cover these areas: 1. FILES: Can you read, search, create, edit, move and delete files right now? 2. PROJECTS: Can you inspect codebases, debug errors, run tests and modify code? 3. PROCESSES: Can you launch applications, inspect running processes, execute PowerShell commands and terminate processes? Explain the limits. 4. DESKTOP: Can you inspect windows, capture screenshots, recognize UI elements, click buttons and type? Which desktop tools are actually callable in this session? 5. COMPLEX TASKS: Can you use TDEL, WorkflowEngine, LocalOperationsSupervisor and SubagentManager? 6. AI MODELS: Which model handles conversation, planning, local execution and final synthesis? What is processed locally and what information is transmitted to cloud providers? 7. APPROVALS: Which actions require my approval? Is there an actual user-facing approval mechanism in this TUI? 8. AVAILABILITY: Identify capabilities that are implemented in the code but unavailable in this session. For every capability, distinguish IMPLEMENTED, REGISTERED, AVAILABLE NOW and LIVE-VERIFIED. Only claim something is live-verified if you have actually exercised it successfully. Use evidence from the current runtime registry where possible. Do not guess. Finish with five concrete examples of tasks I can give you right now."""

CONVERSATIONAL_PROMPTS = [
    FAILED_CAPABILITY_AUDIT_PROMPT,
    "What can you do with my files?",
    "Explain how you would debug this project.",
    "What are your capabilities? Explain whether you can inspect projects, run tests and operate Windows.",
    "Can you read, search, create, edit, move and delete files right now?",
    "How does Presence coordinate local and cloud models?",
    "Tell me about the architecture of Presence.",
    "If I wanted to run pytest on this codebase, what command would you use?",
    "What difference does LocalInferenceSemaphore make?",
    "Give me advice on structuring my desktop interaction.",
    "Why is Gemini used for planning while Qwen runs locally?",
    "What would happen if an MCP server crashes?",
    "Can you inspect codebases, debug errors, run tests and modify code?",
    "Which desktop tools are actually callable in this session?",
    "Can you use TDEL, WorkflowEngine, LocalOperationsSupervisor and SubagentManager?",
    "Which actions require my approval in the TUI?",
    "How does the PermissionBoundary enforce authority tiers?",
    "Describe the CloudReturnPacket compaction process.",
    "Explain the difference between L0_OBSERVE and L2_EXECUTE_REVERSIBLE.",
    "What is the role of the Windows Job Object in ProcessManager?",
    "What are the advantages of using UIA fast-path over OCR?",
    "Can you explain how Thompson sampling selects providers?",
    "What is the purpose of the EventBus in Presence?",
    "Explain how task ledger SQLite checkpoints provide recovery.",
    "Describe how desktop action verification confirms state changes.",
    "This is a question, not a task: can you run tests or execute commands?",
    "Do not execute any tools, just tell me what commands are available.",
    "Without running anything, describe how you delete unnecessary files.",
    "What happens when I say 'delete all files' in chat?",
]

OPERATIONAL_PROMPTS = [
    "Could you inspect my project and identify the failing tests?",
    "Run the tests in this project and report failures.",
    "Audit installed applications and find compatible MCPs for Blender and Chrome.",
    "Search GitHub for Davinci Resolve MCP servers and list the top 3.",
    "Perform a full system diagnostic and check for GPU compute bottlenecks.",
    "Run pytest on tests/test_tdel_integration.py and report results.",
    "Inspect this repository structure, package manifests, and recent git history.",
]

PLAN_ONLY_PROMPTS = [
    "Create a plan for updating the project, but do not execute it.",
    "Draft a plan to migrate the tests, plan only.",
    "Generate a plan to clean up unnecessary files, but do not run it.",
]


class TestConversationalRouting:
    """Verifies deterministic conversation-first intent classification."""

    @pytest.mark.parametrize("prompt", CONVERSATIONAL_PROMPTS)
    def test_conversational_prompts_bypass_decomposition(self, prompt):
        is_disc = ComplexityClassifier.is_discussion_only(prompt)
        cat = ComplexityClassifier.classify(prompt)
        req = ComplexityClassifier.requires_decomposition(cat)

        assert is_disc is True, f"Expected discussion-only for: {prompt[:60]}"
        assert cat == ComplexityCategory.SIMPLE, f"Expected SIMPLE for: {prompt[:60]}, got {cat}"
        assert req is False, f"Expected no decomposition required for: {prompt[:60]}"

    @pytest.mark.parametrize("prompt", OPERATIONAL_PROMPTS)
    def test_operational_prompts_require_decomposition(self, prompt):
        is_disc = ComplexityClassifier.is_discussion_only(prompt)
        cat = ComplexityClassifier.classify(prompt)
        req = ComplexityClassifier.requires_decomposition(cat)

        assert is_disc is False, f"Expected operational (not discussion-only) for: {prompt[:60]}"
        assert req is True, f"Expected decomposition required for: {prompt[:60]}"

    @pytest.mark.parametrize("prompt", PLAN_ONLY_PROMPTS)
    def test_plan_only_prompts_recognized(self, prompt):
        assert ComplexityClassifier.is_plan_only(prompt) is True

    def test_original_failed_capability_audit_prompt_does_not_emit_plan(self):
        """Regression test specifically for the user prompt that failed in the incident."""
        prompt = FAILED_CAPABILITY_AUDIT_PROMPT

        assert ComplexityClassifier.is_discussion_only(prompt) is True
        cat = ComplexityClassifier.classify(prompt)
        assert cat == ComplexityCategory.SIMPLE
        assert ComplexityClassifier.requires_decomposition(cat) is False

    def test_session_execute_turn_bypasses_tdel_for_conversational_prompt(self, tmp_path):
        """Verifies AgentSession.execute_turn routes conversational query to model without PLAN_CREATED."""
        from runtime.trajectory import TrajectoryStore
        from runtime.journal import EventJournal
        from runtime.summaries import GlobalSummaryManager
        from runtime.conversation import ConversationMetadata
        from runtime.tools.registry import ToolRegistry
        from runtime.permissions import PermissionBoundary
        from runtime.providers.fake import FakeModelProvider
        from runtime.providers.base import ModelStreamChunk

        cid = "test-conv-routing-001"
        meta = ConversationMetadata(conversation_id=cid, title="Test Routing")
        store = TrajectoryStore(conversation_id=cid, db_dir=str(tmp_path))
        store.save_metadata(meta)
        journal = EventJournal(conversation_id=cid, logs_dir=tmp_path)
        summary_mgr = GlobalSummaryManager(db_path=tmp_path / "summary.db")
        tool_reg = ToolRegistry()
        boundary = PermissionBoundary()
        provider = FakeModelProvider(
            scripted_responses=[[ModelStreamChunk(text_delta="I can answer your questions directly.")]]
        )

        # Mock TaskPlanningService to assert it is NEVER called
        mock_planning_service = MagicMock()

        from runtime.session import AgentSession
        from runtime.agent_executor import AgentExecutor
        from runtime.reconstruction import ContextAssembler

        assembler = ContextAssembler(trajectory_store=store, summary_manager=summary_mgr)
        executor = AgentExecutor(
            trajectory_store=store,
            event_journal=journal,
            summary_manager=summary_mgr,
            permission_boundary=boundary,
            tool_registry=tool_reg,
            model_provider=provider,
            context_assembler=assembler,
        )

        session = AgentSession(
            conversation_id=cid,
            metadata=meta,
            trajectory_store=store,
            event_journal=journal,
            summary_manager=summary_mgr,
            tool_registry=tool_reg,
            permission_boundary=boundary,
            model_provider=provider,
            agent_executor=executor,
            context_assembler=assembler,
            task_planning_service=mock_planning_service,
        )

        events = list(session.execute_turn(FAILED_CAPABILITY_AUDIT_PROMPT))
        event_types = [e.event_type for e in events]

        # Verify NO plan was created or executed
        assert "PLAN_CREATED" not in event_types
        assert "PLAN_COMPLETED" not in event_types
        assert "PLAN_FAILED" not in event_types
        mock_planning_service.evaluate_and_execute.assert_not_called()

        # Verify conversational text was returned
        assert "TEXT_DELTA" in event_types
        text = "".join(e.payload.get("delta", "") for e in events if e.event_type == "TEXT_DELTA")
        assert "I can answer your questions directly." in text
