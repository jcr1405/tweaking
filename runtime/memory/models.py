"""Presence Behavioral Imitation & Personal Memory Models
Defines typed data structures for user preferences, semantic procedures, learned routines,
and episodic execution records.
"""

import time
import uuid
import re
from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional


class ActionSemanticType(str, Enum):
    """Semantic action types for human-like computer operation."""
    LAUNCH_APP = "LAUNCH_APP"
    ACTIVATE_WINDOW = "ACTIVATE_WINDOW"
    INVOKE_ELEMENT = "INVOKE_ELEMENT"
    CLICK_COORDINATE = "CLICK_COORDINATE"
    TYPE_TEXT = "TYPE_TEXT"
    PRESS_SHORTCUT = "PRESS_SHORTCUT"
    PRESS_KEY = "PRESS_KEY"
    NAVIGATE_URL = "NAVIGATE_URL"
    WAIT_CONDITION = "WAIT_CONDITION"
    FILE_OPERATION = "FILE_OPERATION"


@dataclass
class SemanticAction:
    """A single semantic interaction step within a learned routine."""
    action_type: ActionSemanticType
    target: str                                  # Target application, control name, shortcut, or URL
    parameters: Dict[str, Any] = field(default_factory=dict)
    expected_state: Optional[str] = None         # Condition or title expected after execution
    description: str = ""                        # Human-readable step description


@dataclass
class UserPreference:
    """User-specific preference regarding preferred applications, profiles, or shortcuts."""
    key: str                                     # e.g. "browser.default_profile", "preferred_apps.video"
    value: Any                                   # e.g. "Default", "chrome.exe"
    category: str = "general"                    # "browser", "applications", "navigation", "filesystem"
    confidence: float = 1.0                      # Confidence score 0.0 - 1.0
    updated_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LearnedRoutine:
    """A verified or candidate procedural routine for recurring computer-use workflows."""
    routine_id: str
    name: str                                    # Descriptive title of the routine
    trigger_pattern: str                         # Regex or keyword trigger (e.g. r"(?:open|launch|go to)\s+youtube")
    target_app: str                              # Primary application binary (e.g. "chrome.exe")
    actions: List[SemanticAction]                # Ordered sequence of semantic actions
    expected_final_state: str                    # Expected final window title or condition
    initial_state_requirements: Dict[str, Any] = field(default_factory=dict)
    is_approved: bool = True                     # Whether this routine is approved by the user
    confidence: float = 0.9                      # Learning confidence score (0.0 - 1.0)
    execution_count: int = 0
    success_count: int = 0
    last_verified_at: Optional[float] = None
    created_at: float = field(default_factory=time.time)
    description: str = ""

    def matches(self, prompt: str) -> bool:
        """Evaluates whether the incoming user prompt triggers this learned routine."""
        if not prompt or not isinstance(prompt, str):
            return False
        try:
            return bool(re.search(self.trigger_pattern, prompt.strip(), re.IGNORECASE))
        except re.error:
            return self.trigger_pattern.lower() in prompt.lower()


@dataclass
class EpisodicRecord:
    """Record of an executed desktop task, capturing outcome, adaptation, and evidence."""
    record_id: str = field(default_factory=lambda: f"ep_{uuid.uuid4().hex[:10]}")
    objective: str = ""
    routine_id: Optional[str] = None
    outcome: str = "SUCCESS"                     # "SUCCESS", "FAILED", "ADAPTED"
    duration_seconds: float = 0.0
    steps_executed: int = 0
    adaptations_made: int = 0
    evidence_summary: str = ""
    timestamp: float = field(default_factory=time.time)
