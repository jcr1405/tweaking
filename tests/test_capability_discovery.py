"""Tests for Phase 6: TUI Capability Discovery & System Prompt Grounding.
Validates:
1. RuntimeCapabilitySnapshot correctly interrogates the live session.
2. Authority matrix distinguishes IMPLEMENTED, REGISTERED, AVAILABLE NOW, and LIVE-VERIFIED.
3. System prompt snippet grounds conversational turns with verified capabilities.
4. Capability queries do NOT invoke TDEL or create workflows.
"""

import pytest
import tempfile

from runtime.lifecycle import PresenceRuntime
from runtime.capabilities import RuntimeCapabilitySnapshot, CapabilityState
from runtime.task_planning.complexity import ComplexityClassifier


@pytest.fixture
def runtime_fixture():
    with tempfile.TemporaryDirectory() as tmpdir:
        rt = PresenceRuntime(
            mutex_name="Local\\TestCapDiscoveryMutex",
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


def test_capability_snapshot_capture(runtime_fixture):
    """Verifies RuntimeCapabilitySnapshot accurately reflects live session components."""
    session = runtime_fixture.create_session(conversation_id="conv-cap-1")
    snapshot = RuntimeCapabilitySnapshot.capture(session=session, runtime=runtime_fixture)

    assert snapshot.session_role == "cloud_principal"
    assert snapshot.is_cloud_principal is True
    assert "presence.delegate_local_work" in snapshot.registered_tool_ids
    assert "presence.create_plan" in snapshot.registered_tool_ids
    assert snapshot.has_local_supervisor is True
    assert snapshot.has_desktop_controller is True

    # Snippet generation
    snippet = snapshot.build_system_prompt_snippet()
    assert "[AUTHORITATIVE RUNTIME CAPABILITY STATUS]" in snippet
    assert "presence.delegate_local_work" in snippet
    assert "Cloud Principal (You)" in snippet
    assert "Local Operations Supervisor" in snippet


def test_capability_matrix_structure(runtime_fixture):
    """Verifies the four-tier capability matrix separates IMPLEMENTED from REGISTERED and AVAILABLE."""
    session = runtime_fixture.create_session(conversation_id="conv-cap-2")
    snapshot = RuntimeCapabilitySnapshot.capture(session=session, runtime=runtime_fixture)
    matrix = snapshot.to_matrix()

    assert "Files (Read/Search)" in matrix
    assert matrix["Files (Read/Search)"]["IMPLEMENTED"] == "YES"
    assert matrix["Files (Read/Search)"]["REGISTERED"] == "YES (via delegation)"
    assert matrix["Files (Read/Search)"]["AVAILABLE_NOW"] == "YES (scoped to workspace)"
    assert matrix["Files (Read/Search)"]["LIVE_VERIFIED"] == "YES"

    assert "Desktop (Execute Input Actions)" in matrix
    assert matrix["Desktop (Execute Input Actions)"]["AVAILABLE_NOW"] == "YES (consequential clicks require consent)"

    assert "Processes (Terminate)" in matrix
    assert matrix["Processes (Terminate)"]["AVAILABLE_NOW"] == "YES (requires TUI consent token)"


@pytest.mark.parametrize("prompt", [
    "What can you do?",
    "Give me a direct report of your current capabilities in this interactive session.",
    "Can you inspect my files right now?",
    "Which desktop tools are actually callable in this session?",
    "Can you run PowerShell commands or terminate processes?",
    "Explain whether you can inspect codebases, run tests and operate Windows.",
    "Do you have access to TDEL, WorkflowEngine and LocalOperationsSupervisor?",
])
def test_capability_questions_classified_as_discussion_only(prompt):
    """Verifies that capability and status questions are identified as discussion-only and do not invoke TDEL."""
    classifier = ComplexityClassifier()
    assert classifier.is_discussion_only(prompt) is True
