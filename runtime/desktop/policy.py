"""
Presence Desktop Action Policy & Safety Boundary.

Enforces execution boundaries, action-level risk classification,
contextual authorization, dangerous action gating, protected window checks,
and single-use action-bound consent verification.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from runtime.desktop.models import ActionRiskCategory, ActionType, DesktopAction, DesktopWindow
from runtime.permissions import AuthorityLevel, PermissionBoundary, PermissionTier

logger = logging.getLogger(__name__)

# System processes that are strictly off-limits to automated desktop interaction
PROTECTED_PROCESSES: Set[str] = {
    "consent.exe",  # UAC prompt
    "credentialuibroker.exe",  # Windows credential prompt
    "logonui.exe",  # Windows logon screen
    "taskmgr.exe",  # Task manager (kill process risk)
    "regedit.exe",  # Registry editor
    "secpol.msc",
    "gpedit.msc",
}

# Window title keywords indicating security / authentication / sensitive operations
SENSITIVE_TITLE_KEYWORDS: List[str] = [
    "user account control",
    "windows security",
    "enter credentials",
    "smart card",
    "bitlocker",
]

# Sensitive input text keywords (e.g. attempting to type passwords or financial codes)
SENSITIVE_TEXT_PATTERNS: List[re.Pattern] = [
    re.compile(r"password\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"bearer\s+[A-Za-z0-9_\-\.]+", re.IGNORECASE),
    re.compile(r"\b(?:\d[ -]*?){13,16}\b"),  # credit card numbers
]

# Destructive keyword patterns for persistent state changes (Category C)
DESTRUCTIVE_PATTERNS: List[re.Pattern] = [
    re.compile(r"\b(?:delete|remove|format|erase|destroy|overwrite|clear\s+all|drop\s+table|uninstall|wipe|reset\s+settings)\b", re.IGNORECASE),
    re.compile(r"\b(?:empty\s+recycle\s+bin|delete\s+account|format\s+disk)\b", re.IGNORECASE),
]

# Consequential external keyword patterns (Category D)
EXTERNAL_CONSEQUENTIAL_PATTERNS: List[re.Pattern] = [
    re.compile(r"\b(?:send|publish|post|tweet|email|broadcast|submit\s+order|place\s+order)\b", re.IGNORECASE),
    re.compile(r"\b(?:pay|purchase|buy|checkout|wire\s+transfer|send\s+payment|confirm\s+purchase|order\s+now|subscribe)\b", re.IGNORECASE),
]

# Routine / safe navigation patterns (Category B)
ROUTINE_SAFE_PATTERNS: List[re.Pattern] = [
    re.compile(r"\b(?:open\s+options|options|settings|view|nav|tabs?|next|prev|back|forward|refresh|cancel|close\s+dialog|toggle|expand|collapse|select|ok|apply)\b", re.IGNORECASE),
]


class DesktopPolicy:
    """
    Enforces authorization, safety constraints, action-level risk classification,
    and single-use consent verification for desktop operations.
    """

    def __init__(
        self,
        allow_high_impact: bool = False,
        max_batch_size: int = 50,
        allowed_processes: Optional[Set[str]] = None,
        blocked_processes: Optional[Set[str]] = None,
        permission_boundary: Optional[PermissionBoundary] = None,
    ):
        self.allow_high_impact = allow_high_impact
        self.max_batch_size = max_batch_size
        self.allowed_processes = allowed_processes
        self.blocked_processes = (blocked_processes or set()).union(PROTECTED_PROCESSES)
        self.permission_boundary = permission_boundary

    def is_window_protected(self, window: DesktopWindow) -> Tuple[bool, Optional[str]]:
        """Checks if a target window belongs to a protected system or security process."""
        proc_lower = window.process_name.lower().strip()
        if proc_lower in self.blocked_processes:
            return True, f"Target process '{proc_lower}' is protected system infrastructure"

        title_lower = window.title.lower().strip()
        for kw in SENSITIVE_TITLE_KEYWORDS:
            if kw in title_lower:
                return True, f"Target window title '{window.title}' matches sensitive security prompt keyword '{kw}'"

        if self.allowed_processes is not None:
            if proc_lower not in self.allowed_processes:
                return True, f"Process '{proc_lower}' is not in allowed target processes list"

        return False, None

    def classify_action_risk(
        self,
        action: DesktopAction,
        target_window: Optional[DesktopWindow] = None,
        element_metadata: Optional[Dict[str, Any]] = None,
    ) -> Tuple[ActionRiskCategory, str]:
        """
        Classifies an action into risk categories based on contextual evidence:
        target application, window, control name, action primitive, and input parameters.
        """
        # Category A: Pure observation / waiting
        if action.action_type in (ActionType.WAIT_FOR_CONDITION,):
            return ActionRiskCategory.OBSERVATION, "Harmless synchronization/observation"

        # Contextual inspection string combining all available identifiers
        elem_name = (
            action.target_element_name
            or (element_metadata.get("name") if element_metadata else "")
            or ""
        )
        elem_auto_id = (element_metadata.get("automation_id") if element_metadata else "") or ""
        act_text = action.text or ""
        win_title = target_window.title if target_window else ""

        full_context_str = f"{elem_name} {elem_auto_id} {act_text} {win_title}".strip()

        # Check Category D: Consequential External Actions (payments, external messages, orders)
        for pattern in EXTERNAL_CONSEQUENTIAL_PATTERNS:
            if pattern.search(full_context_str):
                return (
                    ActionRiskCategory.CONSEQUENTIAL_EXTERNAL,
                    f"Action matches consequential external operation pattern '{pattern.pattern}' on '{elem_name or 'target'}'",
                )

        # Check Category C: Persistent Destructive Actions (deletions, disk/account modifications)
        for pattern in DESTRUCTIVE_PATTERNS:
            if pattern.search(full_context_str):
                return (
                    ActionRiskCategory.PERSISTENT_DESTRUCTIVE,
                    f"Action matches persistent destructive operation pattern '{pattern.pattern}' on '{elem_name or 'target'}'",
                )

        # Check Category B: Routine Reversible Actions
        # Ordinary control types and navigation patterns
        if action.action_type in (
            ActionType.SCROLL,
            ActionType.MOVE_MOUSE,
            ActionType.SELECT_ITEM,
            ActionType.EXPAND_COLLAPSE,
            ActionType.SET_TOGGLE,
            ActionType.INVOKE_ELEMENT,
        ):
            return ActionRiskCategory.ROUTINE_REVERSIBLE, f"Routine reversible control manipulation ({action.action_type.value})"

        for pattern in ROUTINE_SAFE_PATTERNS:
            if pattern.search(full_context_str):
                return ActionRiskCategory.ROUTINE_REVERSIBLE, f"Matches routine safe UI interaction pattern '{pattern.pattern}'"

        # Safe text entry into non-sensitive field without external words
        if action.action_type in (ActionType.TYPE_TEXT, ActionType.SET_VALUE):
            return ActionRiskCategory.ROUTINE_REVERSIBLE, "Reversible text insertion into field"

        # Click on known control with non-destructive name
        if elem_name and not any(p.search(elem_name) for p in DESTRUCTIVE_PATTERNS + EXTERNAL_CONSEQUENTIAL_PATTERNS):
            return ActionRiskCategory.ROUTINE_REVERSIBLE, f"Click on non-destructive control '{elem_name}'"

        # Coordinate click without identifiable control: check window title
        if action.action_type in (ActionType.CLICK_COORDINATE, ActionType.DOUBLE_CLICK, ActionType.RIGHT_CLICK):
            return ActionRiskCategory.ROUTINE_REVERSIBLE, "Coordinate input within authorized window bounds"

        # Fail-closed for unknown high-impact actions
        return ActionRiskCategory.PERSISTENT_DESTRUCTIVE, f"Unclassified action '{action.action_type}' failed closed to confirmation required"

    def evaluate_action(
        self,
        action: DesktopAction,
        target_window: Optional[DesktopWindow] = None,
        element_metadata: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        Evaluates whether a single desktop action is permitted under safety policy.
        Enforces protected processes, sensitive patterns, risk classification,
        and single-use consent token verification.
        """
        # 1. Protected Process Isolation Check (Fail-closed regardless of consent)
        if target_window:
            is_prot, reason = self.is_window_protected(target_window)
            if is_prot:
                return False, reason

        # 2. Text payload inspection (Passwords, Credit Cards, Bearer Tokens)
        if action.text:
            for pattern in SENSITIVE_TEXT_PATTERNS:
                if pattern.search(action.text):
                    return False, "Input text matches sensitive credential or financial pattern"

        # 3. Action type gating (Scroll distance bounds)
        if action.action_type == ActionType.SCROLL:
            amount = action.scroll_amount or 1
            if amount > 100 or amount < -100:
                return False, f"Scroll amount {amount} exceeds safety limit (-100..100)"

        # 4. Contextual Risk Classification
        risk_category, risk_reason = self.classify_action_risk(
            action=action,
            target_window=target_window,
            element_metadata=element_metadata,
        )

        # 5. Routine and Observation actions are permitted under standard L2 execution
        if risk_category in (ActionRiskCategory.OBSERVATION, ActionRiskCategory.ROUTINE_REVERSIBLE):
            return True, None

        # 6. High-impact / Destructive / Consequential actions require explicit confirmation
        if self.allow_high_impact:
            return True, None

        if not action.consent_token:
            return (
                False,
                f"Action blocked: text or control contains dangerous operation keyword or destructive pattern. Action classified as {risk_category.value}: requires explicit user confirmation. ({risk_reason})",
            )

        # 7. Validate single-use, action-bound consent token via PermissionBoundary
        if not self.permission_boundary:
            return False, "PermissionBoundary required to verify action consent token"

        target_name = (
            action.target_element_name
            or (element_metadata.get("name") if element_metadata else "")
            or ""
        )
        context = {
            "action_id": action.action_id,
            "action_type": action.action_type.value,
            "window_id": action.window_id or (target_window.window_id if target_window else ""),
            "element_id": action.element_id or "",
            "target_name": target_name,
            "text": action.text or "",
            "coordinate": action.coordinate or [],
            "task_id": action.task_id or "",
        }
        cid = action.conversation_id or ""

        # Validate against PermissionBoundary policy and argument hash
        valid = self.permission_boundary.verify_consent_token(
            token=action.consent_token,
            action_name="desktop.execute_actions",
            context=context,
            conversation_id=cid,
        )

        if not valid:
            return (
                False,
                "Consent token verification failed (invalid, expired, already-consumed, or argument mismatch)",
            )

        return True, None

    def evaluate_batch(
        self,
        actions: List[DesktopAction],
        target_window: Optional[DesktopWindow] = None,
        get_element_meta_fn: Optional[Any] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        Evaluates a batch of desktop actions.
        """
        if len(actions) > self.max_batch_size:
            return False, f"Batch size ({len(actions)}) exceeds maximum allowed ({self.max_batch_size})"

        for idx, act in enumerate(actions):
            elem_meta = None
            if get_element_meta_fn and act.element_id:
                elem_meta = get_element_meta_fn(act.element_id)

            allowed, reason = self.evaluate_action(
                action=act,
                target_window=target_window,
                element_metadata=elem_meta,
            )
            if not allowed:
                return False, f"Action [{idx}] ({act.action_id}) rejected: {reason}"

        return True, None
