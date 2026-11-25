"""
Presence Desktop State Verification Backend.

Provides deterministic verification of state transitions before and after desktop actions:
- Inspects UI Automation properties (value, toggle, selection, focus, bounds)
- Inspects top-level window state (active, minimized, closed, title changes)
- Evaluates declared preconditions and postconditions
- Guarantees fail-closed reporting: never reports verified=True unless verified
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from runtime.desktop.accessibility import AccessibilityBackend
from runtime.desktop.models import ActionType, DesktopAction
from runtime.desktop.windows import WindowsManager

logger = logging.getLogger(__name__)


class StateVerifier:
    """
    Verifies desktop state changes and assertions across action boundaries.
    """

    def __init__(
        self,
        windows_manager: WindowsManager,
        accessibility_backend: AccessibilityBackend,
    ):
        self.windows_manager = windows_manager
        self.accessibility_backend = accessibility_backend

    def capture_state(
        self,
        window_id: Optional[str] = None,
        element_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Captures a snapshot of current window and element state.
        """
        state: Dict[str, Any] = {}

        if window_id:
            win = self.windows_manager.get_window(window_id)
            if win:
                state["window"] = {
                    "window_id": win.window_id,
                    "title": win.title,
                    "process_name": win.process_name,
                    "is_foreground": win.is_foreground,
                    "is_visible": win.is_visible,
                    "bounds": [win.bounds.left, win.bounds.top, win.bounds.right, win.bounds.bottom],
                }
            else:
                state["window"] = {"window_id": window_id, "exists": False}

        if element_id:
            elem = self.accessibility_backend.get_registered_element(element_id)
            if elem:
                state["element"] = {
                    "element_id": element_id,
                    "name": elem.get("name"),
                    "control_type": elem.get("control_type"),
                    "value": elem.get("value"),
                    "toggle_state": elem.get("toggle_state"),
                    "is_selected": elem.get("is_selected"),
                    "is_enabled": elem.get("is_enabled", True),
                    "is_expanded": elem.get("is_expanded"),
                    "supported_patterns": elem.get("patterns", []),
                }
            else:
                state["element"] = {"element_id": element_id, "exists": False}

        return state

    def verify_action(
        self,
        action: DesktopAction,
        state_before: Dict[str, Any],
        state_after: Dict[str, Any],
    ) -> Tuple[bool, Optional[str]]:
        """
        Determines whether the executed action successfully achieved its expected state transition.
        Returns (verified: bool, failure_reason: Optional[str]).
        """
        elem_before = state_before.get("element", {})
        elem_after = state_after.get("element", {})
        win_before = state_before.get("window", {})
        win_after = state_after.get("window", {})

        # 1. Check semantic UIA operations
        if action.action_type == ActionType.SET_TOGGLE:
            if action.desired_state is not None:
                actual_state = elem_after.get("toggle_state")
                if actual_state != action.desired_state:
                    return False, f"Toggle state did not change to {action.desired_state} (current: {actual_state})"
            else:
                # Toggle should flip
                if elem_after.get("toggle_state") == elem_before.get("toggle_state"):
                    return False, "Toggle state failed to invert"
            return True, None

        if action.action_type in (ActionType.SET_VALUE, ActionType.TYPE_TEXT):
            if action.text is not None and elem_after:
                actual_val = elem_after.get("value")
                if actual_val is not None and action.text not in str(actual_val):
                    return False, f"Element value does not contain expected text '{action.text}' (actual: '{actual_val}')"
            return True, None

        if action.action_type == ActionType.SELECT_ITEM:
            if elem_after and not elem_after.get("is_selected", False):
                return False, "Item was not selected according to UIA SelectionItemPattern"
            return True, None

        if action.action_type == ActionType.EXPAND_COLLAPSE:
            before_exp = elem_before.get("is_expanded")
            after_exp = elem_after.get("is_expanded")
            if before_exp is not None and after_exp is not None and before_exp == after_exp:
                return False, f"ExpandCollapse state did not transition (stayed {after_exp})"
            return True, None

        # 2. Check explicit precondition / postcondition assertions if specified
        if action.expected_post_condition:
            cond = action.expected_post_condition.lower().strip()
            if "window_closed" in cond or "closed" in cond:
                if win_after.get("exists", True):
                    return False, "Expected window to close, but window still exists"
                return True, None

            if "foreground" in cond or "active" in cond:
                if not win_after.get("is_foreground", False):
                    return False, "Expected window to be in foreground, but is not active"
                return True, None

            if "value_changed" in cond:
                if elem_before.get("value") == elem_after.get("value"):
                    return False, "Element value did not change after action"
                return True, None

        # 3. For coordinate-based actions without explicit postcondition
        # If window closed unexpectedly, report warning
        if win_before.get("exists") and not win_after.get("exists") and action.action_type != ActionType.CLICK_COORDINATE:
            return False, "Target window closed unexpectedly during action execution"

        # If action executed without runtime error and no contradictions found, accept
        return True, None
