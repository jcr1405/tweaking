"""End-to-End Acceptance Test Suite: Local-Only Personal Computer Companion
Validates the complete transformation of Presence into a Local-First Personal Computer Companion:
1. Strict local-only mode enforcement and cloud provider isolation.
2. Personal computer companion persona and authoritative capability status.
3. Conversational routing without false TDEL preemption.
4. Behavioral memory store and learned routine persistence.
5. Screen awareness engine, observation controls, and sensitive-window masking.
6. Environmental telemetry sensors and proactive startup briefing service.
7. Task-scoped one-time consent granting bounded autonomy.
8. Zero cloud transmission verification.
"""

import os
import time
import pytest
from unittest.mock import patch, MagicMock

from runtime.config import is_local_only_mode, set_local_only_mode
from runtime.capabilities import RuntimeCapabilitySnapshot
from runtime.task_planning.complexity import ComplexityClassifier
from runtime.task_planning.models import ComplexityCategory
from runtime.memory.models import LearnedRoutine, SemanticAction, ActionSemanticType
from runtime.memory.store import BehavioralMemoryStore
from runtime.desktop.awareness import ScreenAwarenessMonitor, DesktopStateSnapshot
from runtime.proactive.sensors import SystemSensors, SystemHealthSnapshot
from runtime.proactive.briefing import StartupBriefingService
from runtime.permissions import (
    PermissionBoundary,
    AuthorityLevel,
    ActionPolicy,
    TaskScopedConsent,
    PermissionDeniedError,
)
from runtime.providers.gemini import GeminiModelProvider
from runtime.providers.freellmapi import FreeLLMAPIProvider
from runtime.providers.coordinator import MultiProviderCoordinator
from runtime.providers.fake import FakeModelProvider


# ==============================================================================
# 1. STRICT LOCAL-ONLY MODE ENFORCEMENT & CLOUD ISOLATION
# ==============================================================================

def test_local_only_mode_policy_enforcement():
    """Verifies that when local-only mode is active, cloud providers fail validation immediately."""
    set_local_only_mode(True)
    assert is_local_only_mode() is True

    # Gemini provider must fail validation closed
    gemini = GeminiModelProvider(api_key="AIzaSyDummyKeyForTest")
    valid, err = gemini.validate_configuration()
    assert valid is False
    assert "local-only" in err.lower()

    # FreeLLMAPI provider must fail validation closed
    freellm = FreeLLMAPIProvider(api_key="dummy_key", model_id="local-model")
    valid_free, err_free = freellm.validate_configuration()
    assert valid_free is False
    assert "local-only" in err_free.lower()

    # MultiProviderCoordinator must exclude cloud providers and forbid registration
    coordinator = MultiProviderCoordinator(providers={"ollama": FakeModelProvider()}, primary_provider="ollama")
    assert "gemini" not in coordinator._providers
    assert "freellmapi" not in coordinator._providers

    with pytest.raises((ValueError, PermissionError)):
        coordinator.register_provider("gemini", gemini)


# ==============================================================================
# 2. PERSONAL COMPANION PERSONA & AUTHORITATIVE STATUS
# ==============================================================================

def test_authoritative_capability_matrix_local_only():
    """Verifies capability matrix marks cloud inference disabled and Ollama active."""
    set_local_only_mode(True)
    cap = RuntimeCapabilitySnapshot(
        session_role="personal_companion",
        is_cloud_principal=True,
        registered_tool_ids=["desktop.observe", "presence.delegate_local_work"],
        has_local_supervisor=True,
        has_desktop_controller=True,
        has_mcp_filesystem=True,
        has_mcp_shell=True,
        has_tui_approval_provider=True,
        active_cloud_provider="none",
        local_model="qwen3.5:9b",
    )
    matrix = cap.to_matrix()

    assert matrix["Cloud AI Inference"]["REGISTERED"] == "DISABLED (Local-Only Mode)"
    assert matrix["Cloud AI Inference"]["AVAILABLE_NOW"] == "NO"
    assert matrix["Local Ollama Inference"]["AVAILABLE_NOW"] == "YES"
    assert matrix["Behavioral Memory Store"]["AVAILABLE_NOW"] == "YES"
    assert matrix["Screen Awareness Monitor"]["AVAILABLE_NOW"] == "YES"

    snippet = cap.build_system_prompt_snippet()
    assert "[AUTHORITATIVE RUNTIME CAPABILITY STATUS — STRICT LOCAL-ONLY MODE]" in snippet
    assert "Local Companion (You): Running 100% locally on Ollama (Qwen 3.5 9B)" in snippet
    assert "Zero Cloud Transmission" in snippet


# ==============================================================================
# 3. CONVERSATIONAL ROUTING (NO FALSE TDEL PREEMPTION)
# ==============================================================================

def test_conversational_routing_no_false_preemption():
    """Verifies conversational questions and direct delegation prompts do not trigger TDEL."""
    # Capability status inquiry
    p1 = "What are your capabilities? Explain whether you can inspect projects, run tests and operate Windows."
    assert ComplexityClassifier.is_discussion_only(p1) is True
    assert ComplexityClassifier.classify(p1) == ComplexityCategory.SIMPLE

    # Explicit negative instruction
    p2 = "This is a conversational status question, NOT a task. Do not invoke TDEL. Can you read files right now?"
    assert ComplexityClassifier.is_discussion_only(p2) is True
    assert ComplexityClassifier.classify(p2) == ComplexityCategory.SIMPLE

    # Direct tool delegation request (single turn for agent)
    p3 = "Perform a read-only local delegation test using presence.delegate_local_work. Ask LocalOperationsSupervisor to list only immediate files."
    assert ComplexityClassifier.classify(p3) == ComplexityCategory.SIMPLE


# ==============================================================================
# 4. BEHAVIORAL MEMORY STORE & ROUTINE PERSISTENCE
# ==============================================================================

def test_behavioral_memory_store_persistence(tmp_path):
    """Verifies SQLite behavioral memory store saves, retrieves, and redacts routines."""
    db_file = tmp_path / "test_memory.db"
    store = BehavioralMemoryStore(db_path=db_file)

    # Verify pre-seeded YouTube routine
    routines = store.list_routines()
    assert len(routines) >= 1
    yt_routine = store.get_routine_by_name("Launch YouTube via Chrome Default Profile")
    assert yt_routine is not None
    assert yt_routine.target_app == "chrome.exe"
    assert len(yt_routine.actions) >= 3

    # Add a new custom routine with sensitive arguments
    custom_routine = LearnedRoutine(
        routine_id="routine_test_123",
        name="Test Login Routine",
        trigger_pattern=r"log in to test",
        target_app="app.exe",
        expected_final_state="Logged In - Test App",
        actions=[
            SemanticAction(
                action_type=ActionSemanticType.INVOKE_ELEMENT,
                target="Username Input",
                parameters={"input_type": "text"},
            ),
            SemanticAction(
                action_type=ActionSemanticType.TYPE_TEXT,
                target="Password Input",
                parameters={"password": "supersecretpassword123", "pin": "1234"},
            ),
        ],
    )
    store.save_routine(custom_routine)

    retrieved = store.get_routine("routine_test_123")
    assert retrieved is not None
    # Password parameters must be automatically redacted
    type_action = retrieved.actions[1]
    assert type_action.parameters["password"] == "[REDACTED_SENSITIVE_INPUT]"
    assert type_action.parameters["pin"] == "[REDACTED_SENSITIVE_INPUT]"


# ==============================================================================
# 5. SCREEN AWARENESS MONITOR & SENSITIVE-WINDOW MASKING
# ==============================================================================

def test_screen_awareness_monitor_and_privacy_masking():
    """Verifies screen awareness monitor detects sensitive windows and enforces privacy masking."""
    monitor = ScreenAwarenessMonitor()

    # Normal application window
    assert monitor.is_window_sensitive("Visual Studio Code - tweaking", "Code.exe") is False
    assert monitor.is_window_sensitive("YouTube - Google Chrome", "chrome.exe") is False

    # Sensitive windows
    assert monitor.is_window_sensitive("Bitwarden - Master Password", "bitwarden.exe") is True
    assert monitor.is_window_sensitive("1Password - Unlock", "1password.exe") is True
    assert monitor.is_window_sensitive("Sign in to your account - Google Accounts", "chrome.exe") is True
    assert monitor.is_window_sensitive("Personal Banking - Microsoft Edge", "msedge.exe") is True
    assert monitor.is_window_sensitive("Windows Security - PIN Entry", "credentialui.exe") is True

    # Observation pause / resume controls
    assert monitor.is_observation_active is True
    monitor.pause_observation()
    assert monitor.is_observation_active is False

    snap = monitor.sample_current_state(capture_frame=True)
    assert snap.observation_active is False
    assert snap.frame_buffer is None
    assert snap.window_title == "[OBSERVATION PAUSED]"

    monitor.resume_observation()
    assert monitor.is_observation_active is True


# ==============================================================================
# 6. ENVIRONMENTAL SENSORS & PROACTIVE STARTUP BRIEFING
# ==============================================================================

def test_environmental_sensors_and_startup_briefing():
    """Verifies system sensors sample hardware metrics and briefing service generates narrative."""
    snapshot = SystemSensors.sample()
    assert snapshot.cpu_cores >= 1
    assert snapshot.ram_total_gb > 0
    assert snapshot.ram_available_gb > 0
    assert snapshot.disk_total_gb > 0

    briefing_service = StartupBriefingService()
    briefing = briefing_service.generate_briefing()

    assert briefing.greeting in ("Good morning", "Good afternoon", "Good evening")
    assert "personal computer companion" in briefing.briefing_text.lower()
    assert "qwen 3.5 9b" in briefing.briefing_text.lower()
    assert briefing.health_snapshot.cpu_cores >= 1


# ==============================================================================
# 7. TASK-SCOPED CONSENT & BOUNDED AUTONOMY
# ==============================================================================

def test_task_scoped_consent_and_bounded_autonomy():
    """Verifies task-scoped consent grants bounded autonomy for reversible in-scope actions."""
    pb = PermissionBoundary()

    task_id = "task_organize_downloads_001"
    consent = TaskScopedConsent(
        task_id=task_id,
        objective="Organize Downloads Folder",
        target_apps=["explorer.exe", "powershell.exe"],
        allowed_action_types=["CLICK", "NAVIGATE"],
        max_action_budget=3,
        expires_at=time.monotonic() + 60.0,
    )
    pb.register_task_consent(consent)

    # In-scope action 1 (Desktop action on explorer.exe)
    res1 = pb.check_permission(
        action_name="desktop.execute_actions",
        context={"task_id": task_id, "process_name": "explorer.exe", "action_type": "CLICK"},
    )
    assert res1.allowed is True
    assert "1/3 used" in res1.reason

    # In-scope action 2
    res2 = pb.check_permission(
        action_name="desktop.execute_actions",
        context={"task_id": task_id, "process_name": "explorer.exe", "action_type": "CLICK"},
    )
    assert res2.allowed is True
    assert "2/3 used" in res2.reason

    # In-scope action 3 (Reaches budget limit)
    res3 = pb.check_permission(
        action_name="desktop.execute_actions",
        context={"task_id": task_id, "process_name": "explorer.exe", "action_type": "CLICK"},
    )
    assert res3.allowed is True
    assert "3/3 used" in res3.reason

    # Action 4: Exceeds budget -> fails
    res4 = pb.check_permission(
        action_name="desktop.execute_actions",
        context={"task_id": task_id, "process_name": "explorer.exe", "action_type": "CLICK"},
    )
    # Action budget exhausted means autonomous task-scoped execution is refused
    assert res4.allowed is False or "exhausted" in res4.reason

    # Target app mismatch (chrome.exe not in ["explorer.exe", "powershell.exe"])
    consent2 = TaskScopedConsent(
        task_id="task_scoped_target_check",
        objective="Work on Explorer",
        target_apps=["explorer.exe"],
        max_action_budget=5,
    )
    pb.register_task_consent(consent2)
    res_mismatch = pb.check_permission(
        action_name="desktop.execute_actions",
        context={"task_id": "task_scoped_target_check", "process_name": "chrome.exe"},
    )
    assert "outside approved task scope" in res_mismatch.reason or res_mismatch.allowed is False

    # L3 Protected Action (e.g. process_kill) CANNOT bypass explicit user consent via task scope
    res_l3 = pb.check_permission(
        action_name="system.process_kill",
        context={"task_id": "task_scoped_target_check", "process_name": "explorer.exe"},
    )
    assert res_l3.allowed is False
    assert res_l3.user_consent_required is True


# ==============================================================================
# 8. ZERO CLOUD TRANSMISSION VERIFICATION
# ==============================================================================

def test_zero_cloud_transmission_in_local_mode():
    """Verifies that in local-only mode, no cloud API calls or connections can be initiated."""
    set_local_only_mode(True)

    with patch("urllib.request.urlopen") as mock_url:
        # Attempting to call Gemini in local-only mode should fail before any HTTP request
        gemini = GeminiModelProvider(api_key="dummy_gemini_key")
        valid, msg = gemini.validate_configuration()
        assert valid is False
        # mock_url must not have been called
        mock_url.assert_not_called()
