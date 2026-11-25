"""User Consent & Consequential Action Approval Modal Dialog.
Provides a clear, user-facing approval interface for protected, consequential,
or destructive operations across Windows desktop, filesystem, and processes.
"""

import time
import json
from typing import Dict, Any, Optional
from rich.text import Text
from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Static, Button, Label
from textual.containers import Vertical, Horizontal, ScrollableContainer
from textual.binding import Binding


class ConsentApprovalModal(ModalScreen[bool]):
    """Modal dialog displaying consequential action details and awaiting explicit user consent."""

    BINDINGS = [
        Binding("a", "approve", "Approve", show=True),
        Binding("r", "reject", "Reject", show=True),
        Binding("escape", "reject", "Reject", show=True),
    ]

    def __init__(
        self,
        action_name: str,
        context: Optional[Dict[str, Any]] = None,
        reason: Optional[str] = None,
        required_tier: str = "L3_PROTECTED",
        is_reversible: bool = False,
        timeout_seconds: float = 60.0,
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        self.action_name = action_name
        self.context = context or {}
        self.reason = reason or "Operation requires explicit user authorization."
        self.required_tier = required_tier
        self.is_reversible = is_reversible
        self.timeout_seconds = timeout_seconds
        self.start_time = time.monotonic()

    def compose(self) -> ComposeResult:
        # Extract meaningful target identification
        target_info = (
            self.context.get("target_name")
            or self.context.get("path")
            or self.context.get("window_id")
            or self.context.get("command")
            or self.context.get("process_name")
            or self.context.get("target")
            or "System Resource"
        )
        side_effects = (
            "No state alteration (Reversible)"
            if self.is_reversible
            else "Permanent / Consequential state change (Irreversible)"
        )

        with Vertical(id="consent-dialog"):
            yield Static("⚠️ AUTHORIZATION REQUIRED", id="consent-dialog-title")
            yield Static(
                "Presence intercepted a protected or consequential operation that cannot execute\n"
                "without your explicit consent. Review the parameters below before approving.",
                classes="dialog-desc",
            )

            with ScrollableContainer(id="consent-details-scroll"):
                yield Label(f"Operation: {self.action_name}", classes="consent-item-bold")
                yield Label(f"Authority Tier: {self.required_tier}", classes="consent-item")
                yield Label(f"Target Resource: {target_info}", classes="consent-item")
                yield Label(f"Expected Side Effects: {side_effects}", classes="consent-item")
                yield Label(f"Justification: {self.reason}", classes="consent-item-reason")

                yield Label("Relevant Parameters:", classes="consent-param-title")
                param_text = json.dumps(self.context, indent=2) if self.context else "{}"
                yield Static(param_text, classes="consent-params-box")

            yield Static(f"⏳ Expiry: This request expires in {int(self.timeout_seconds)}s", id="consent-expiry-notice")

            with Horizontal(id="dialog-buttons"):
                yield Button("✓ [A] Approve", id="btn-consent-approve", variant="success")
                yield Button("✗ [R] Reject", id="btn-consent-reject", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-consent-approve":
            self.action_approve()
        elif event.button.id == "btn-consent-reject":
            self.action_reject()

    def action_approve(self) -> None:
        """Approve the operation."""
        now = time.monotonic()
        if now - self.start_time > self.timeout_seconds:
            self.dismiss(False)
            return
        self.dismiss(True)

    def action_reject(self) -> None:
        """Reject the operation."""
        self.dismiss(False)
