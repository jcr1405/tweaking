"""Tests for Phase 4: User-Facing TUI Approval Flow and Cryptographic Consent Verification.
Validates:
1. ConsentApprovalModal displays operation, parameters, risk tier, justification, and expiry.
2. TUIApprovalProvider coordinates with PermissionBoundary to issue single-use tokens.
3. Cryptographic tokens enforce argument digest binding, conversation binding, and single-use consumption.
4. Tampered arguments or cross-conversation token presentation fails closed.
5. Expired tokens are rejected.
6. WorkflowEngine cleanly pauses in WAITING_FOR_APPROVAL / AWAITING_APPROVAL.
7. WorkflowEngine.resume_workflow() resumes execution with valid consent token.
8. WorkflowEngine.reject_workflow_approval() cancels waiting nodes safely.
"""

import time
import pytest
from unittest.mock import MagicMock

from runtime.permissions import (
    PermissionBoundary,
    ActionPolicy,
    AuthorityLevel,
)
from runtime.tui.consent_modal import ConsentApprovalModal
from runtime.tui.approval_provider import TUIApprovalProvider
from runtime.workflows.models import (
    WorkflowDefinition,
    WorkflowNode,
    ExecutionType,
    NodeState,
    WorkflowStatus,
)
from runtime.workflows.engine import WorkflowEngine
from runtime.desktop.controller import DesktopController


def test_consent_approval_modal_attributes():
    """Verifies ConsentApprovalModal correctly encapsulates operation details."""
    ctx = {"path": "C:/secret/file.txt", "action": "delete"}
    modal = ConsentApprovalModal(
        action_name="fs.delete_file",
        context=ctx,
        reason="User requested file deletion",
        required_tier="L3_PROTECTED",
        is_reversible=False,
        timeout_seconds=30.0,
    )
    assert modal.action_name == "fs.delete_file"
    assert modal.context == ctx
    assert modal.reason == "User requested file deletion"
    assert modal.required_tier == "L3_PROTECTED"
    assert modal.is_reversible is False


def test_tui_approval_provider_approve_flow():
    """Verifies TUIApprovalProvider issues a valid token upon explicit user approval."""
    boundary = PermissionBoundary()
    boundary.register_policy(
        ActionPolicy(
            action_name="system.process_kill",
            required_level=AuthorityLevel.L3_PROTECTED,
            description="Terminate external process",
            requires_explicit_consent=True,
        )
    )

    mock_app = MagicMock()
    # Mock push_screen to invoke the callback with approved=True
    def mock_push_screen(modal, callback=None):
        if callback:
            callback(True)

    mock_app.push_screen.side_effect = mock_push_screen
    mock_app.safe_call = lambda fn, *args: fn(*args)

    provider = TUIApprovalProvider(app=mock_app, permission_boundary=boundary)

    ctx = {"pid": 1234, "process_name": "notepad.exe"}
    token = provider.issue_token_on_approval(
        action_name="system.process_kill",
        context=ctx,
        conversation_id="conv-1",
        reason="Terminate stuck editor",
        ttl_seconds=60.0,
    )

    assert token is not None
    assert token.startswith("cst_")

    # Token must verify successfully with matching args
    valid = boundary.verify_consent_token(
        token=token,
        action_name="system.process_kill",
        context=ctx,
        conversation_id="conv-1",
    )
    assert valid is True

    # Token must be single-use (second verification fails)
    replayed = boundary.verify_consent_token(
        token=token,
        action_name="system.process_kill",
        context=ctx,
        conversation_id="conv-1",
    )
    assert replayed is False


def test_tui_approval_provider_reject_flow():
    """Verifies TUIApprovalProvider returns None and fails closed when user rejects."""
    boundary = PermissionBoundary()
    mock_app = MagicMock()

    def mock_push_screen(modal, callback=None):
        if callback:
            callback(False)

    mock_app.push_screen.side_effect = mock_push_screen
    mock_app.safe_call = lambda fn, *args: fn(*args)

    provider = TUIApprovalProvider(app=mock_app, permission_boundary=boundary)

    ctx = {"pid": 5678}
    token = provider.issue_token_on_approval(
        action_name="system.process_kill",
        context=ctx,
        conversation_id="conv-1",
        reason="Terminate system service",
    )
    assert token is None


def test_consent_token_tampering_and_mismatch():
    """Verifies consent token fails when presented with altered arguments or mismatched conversation."""
    boundary = PermissionBoundary()
    boundary.register_policy(
        ActionPolicy(
            action_name="fs.delete_file",
            required_level=AuthorityLevel.L3_PROTECTED,
            description="Delete file from filesystem",
            requires_explicit_consent=True,
        )
    )

    ctx = {"path": "C:/safe/file.txt"}
    token = boundary.issue_consent_token(
        action_name="fs.delete_file",
        context=ctx,
        conversation_id="conv-good",
        ttl_seconds=60.0,
    )

    # 1. Tampered arguments fail
    tampered_ctx = {"path": "C:/Windows/System32/config"}
    assert boundary.verify_consent_token(
        token=token,
        action_name="fs.delete_file",
        context=tampered_ctx,
        conversation_id="conv-good",
    ) is False

    # 2. Issue new token and test cross-conversation presentation
    token2 = boundary.issue_consent_token(
        action_name="fs.delete_file",
        context=ctx,
        conversation_id="conv-good",
        ttl_seconds=60.0,
    )
    assert boundary.verify_consent_token(
        token=token2,
        action_name="fs.delete_file",
        context=ctx,
        conversation_id="conv-malicious-other",
    ) is False


def test_consent_token_expiration():
    """Verifies expired consent tokens fail closed."""
    boundary = PermissionBoundary()
    boundary.register_policy(
        ActionPolicy(
            action_name="fs.delete_file",
            required_level=AuthorityLevel.L3_PROTECTED,
            description="Delete file from filesystem",
            requires_explicit_consent=True,
        )
    )

    ctx = {"path": "C:/test.txt"}
    token = boundary.issue_consent_token(
        action_name="fs.delete_file",
        context=ctx,
        conversation_id="conv-exp",
        ttl_seconds=0.01,
    )
    time.sleep(0.05)

    assert boundary.verify_consent_token(
        token=token,
        action_name="fs.delete_file",
        context=ctx,
        conversation_id="conv-exp",
    ) is False


def test_workflow_engine_awaiting_approval_and_resumption():
    """Verifies WorkflowEngine pauses in AWAITING_APPROVAL and resumes on approval."""
    boundary = PermissionBoundary()
    desktop_ctrl = DesktopController(dry_run=True)
    engine = WorkflowEngine(permission_boundary=boundary, desktop_controller=desktop_ctrl)

    # Construct workflow where node 1 executes normally, node 2 needs consent
    wf_def = WorkflowDefinition(
        workflow_id="wf-approval-test",
        name="Approval Flow Test",
        nodes=[
            WorkflowNode(
                node_id="n1",
                name="Safe Wait Action",
                execution_type=ExecutionType.DESKTOP_OPERATION,
                payload={"action_type": "wait_for_condition"},
            ),
            WorkflowNode(
                node_id="n2",
                name="Desktop Action Awaiting Consent",
                execution_type=ExecutionType.DESKTOP_OPERATION,
                payload={
                    "action_type": "click_coordinate",
                    "coordinate": [100, 200],
                    "target_element_name": "Delete All Data",
                    "side_effecting": True,
                },
                depends_on=["n1"],
            ),
        ],
    )

    # First run without consent token: n1 succeeds, n2 pauses in WAITING_FOR_APPROVAL
    run = engine.execute_workflow(
        definition=wf_def,
        conversation_id="conv-wf-appr",
    )

    assert run.status == WorkflowStatus.AWAITING_APPROVAL
    assert run.node_executions["n1"].state == NodeState.COMPLETED
    assert run.node_executions["n2"].state == NodeState.WAITING_FOR_APPROVAL

    # Issue token for n2
    token = boundary.issue_consent_token(
        action_name="desktop.execute_actions",
        context={
            "action_id": f"act_n2",
            "action_type": "click_coordinate",
            "window_id": "",
            "element_id": "",
            "target_name": "Delete All Data",
            "text": "",
            "coordinate": (100, 200),
            "task_id": "n2",
        },
        conversation_id="conv-wf-appr",
        ttl_seconds=60.0,
    )

    # Resume workflow with consent token
    resumed_run = engine.resume_workflow(
        run_id=run.run_id,
        consent_tokens={"n2": token},
    )

    # n2 should have dispatched and completed
    assert resumed_run.status == WorkflowStatus.COMPLETED
    assert resumed_run.node_executions["n2"].state == NodeState.COMPLETED


def test_workflow_engine_rejection_flow():
    """Verifies WorkflowEngine cleanly cancels waiting nodes upon rejection."""
    desktop_ctrl = DesktopController(dry_run=True)
    engine = WorkflowEngine(desktop_controller=desktop_ctrl)

    wf_def = WorkflowDefinition(
        workflow_id="wf-reject-test",
        name="Rejection Flow Test",
        nodes=[
            WorkflowNode(
                node_id="n1",
                name="High Risk Action",
                execution_type=ExecutionType.DESKTOP_OPERATION,
                payload={
                    "action_type": "click_coordinate",
                    "coordinate": [50, 50],
                    "target_element_name": "Format Drive",
                    "side_effecting": True,
                },
            ),
        ],
    )

    run = engine.execute_workflow(definition=wf_def, conversation_id="conv-rej")
    assert run.status == WorkflowStatus.AWAITING_APPROVAL
    assert run.node_executions["n1"].state == NodeState.WAITING_FOR_APPROVAL

    rejected_run = engine.reject_workflow_approval(run.run_id, reason="User clicked Reject")
    assert rejected_run.status == WorkflowStatus.CANCELLED
    assert rejected_run.node_executions["n1"].state == NodeState.CANCELLED
    assert rejected_run.node_executions["n1"].state == NodeState.CANCELLED
