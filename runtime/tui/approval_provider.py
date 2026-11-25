"""TUI Approval Provider.
Bridges PermissionBoundary and WorkflowEngine to the Textual TUI via interactive consent modals.
Guarantees single-use cryptographically bound token issuance upon real human approval.
"""

import time
import logging
import threading
from typing import Dict, Any, Optional, Callable

from runtime.permissions import PermissionBoundary, AuthorityLevel

logger = logging.getLogger("presence.tui.approval")


class TUIApprovalProvider:
    """Coordinates user consent interactions between background execution threads and the TUI event loop."""

    def __init__(
        self,
        app: Optional[Any] = None,
        permission_boundary: Optional[PermissionBoundary] = None,
        default_timeout_seconds: float = 60.0,
    ):
        self.app = app
        self.permission_boundary = permission_boundary
        self.default_timeout_seconds = default_timeout_seconds
        self._lock = threading.Lock()

    def set_app(self, app: Any) -> None:
        self.app = app

    def set_permission_boundary(self, boundary: PermissionBoundary) -> None:
        self.permission_boundary = boundary

    def request_approval(
        self,
        action_name: str,
        context: Optional[Dict[str, Any]] = None,
        reason: Optional[str] = None,
        required_tier: str = "L3_PROTECTED",
        is_reversible: bool = False,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        """Prompts the user via ConsentApprovalModal and blocks the caller thread until resolved.
        Fails closed (returns False) on rejection, timeout, or missing app reference.
        """
        if not self.app:
            logger.warning(f"No active TUI app attached to TUIApprovalProvider; rejecting '{action_name}' by default.")
            return False

        timeout = timeout_seconds or self.default_timeout_seconds
        event = threading.Event()
        result_box = {"approved": False}

        def _on_modal_result(approved: Optional[bool]) -> None:
            result_box["approved"] = bool(approved)
            event.set()

        def _show_modal() -> None:
            try:
                from runtime.tui.consent_modal import ConsentApprovalModal
                modal = ConsentApprovalModal(
                    action_name=action_name,
                    context=context or {},
                    reason=reason,
                    required_tier=required_tier,
                    is_reversible=is_reversible,
                    timeout_seconds=timeout,
                )
                self.app.push_screen(modal, callback=_on_modal_result)
            except Exception as e:
                logger.error(f"Failed to push ConsentApprovalModal: {e}")
                event.set()

        # Dispatch modal to TUI event loop
        if hasattr(self.app, "safe_call"):
            self.app.safe_call(_show_modal)
        elif hasattr(self.app, "call_from_thread"):
            self.app.call_from_thread(_show_modal)
        else:
            _show_modal()

        # Block background execution worker until user acts or timeout
        signaled = event.wait(timeout=timeout)
        if not signaled:
            logger.warning(f"Consent request for '{action_name}' timed out after {timeout}s. Failing closed.")
            return False

        approved = result_box["approved"]
        if approved:
            logger.info(f"User EXPLICITLY APPROVED operation '{action_name}'.")
        else:
            logger.info(f"User REJECTED operation '{action_name}'.")
        return approved

    def issue_token_on_approval(
        self,
        action_name: str,
        context: Optional[Dict[str, Any]] = None,
        conversation_id: str = "",
        reason: Optional[str] = None,
        ttl_seconds: float = 60.0,
    ) -> Optional[str]:
        """Interactively requests approval, and if approved, issues a single-use cryptographically bound token."""
        approved = self.request_approval(
            action_name=action_name,
            context=context,
            reason=reason,
            timeout_seconds=ttl_seconds,
        )
        if not approved:
            return None

        if not self.permission_boundary:
            logger.warning("No PermissionBoundary attached; cannot issue consent token.")
            return None

        token = self.permission_boundary.issue_consent_token(
            action_name=action_name,
            context=context,
            conversation_id=conversation_id,
            ttl_seconds=ttl_seconds,
        )
        return token

    def __call__(self, action_name: str, context: Optional[Dict[str, Any]] = None) -> bool:
        """Standard ConsentProvider callback interface for PermissionBoundary."""
        return self.request_approval(action_name=action_name, context=context)
