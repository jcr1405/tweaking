"""
Presence Desktop Automation & Vision-Guided Computer Use Package.
"""

from runtime.desktop.accessibility import AccessibilityBackend
from runtime.desktop.capture import CapturedScreenshot, ScreenCaptureBackend
from runtime.desktop.controller import DesktopController
from runtime.desktop.input import DesktopInputBackend
from runtime.desktop.harness import PresenceDesktopHarness
from runtime.desktop.models import (
    ActionRiskCategory,
    ActionStatus,
    ActionType,
    DesktopAction,
    DesktopActionResult,
    DesktopBatchResult,
    DesktopElement,
    DesktopObservation,
    DesktopWindow,
    ObservationSource,
    ScreenRect,
    VisualElementCandidate,
    VisualPerceptionResult,
)
from runtime.desktop.perception import VisionPerception
from runtime.desktop.policy import DesktopPolicy
from runtime.desktop.verification import StateVerifier
from runtime.desktop.windows import WindowsManager
from runtime.desktop.awareness import ScreenAwarenessMonitor, DesktopStateSnapshot

__all__ = [
    "AccessibilityBackend",
    "ActionRiskCategory",
    "ActionStatus",
    "ActionType",
    "CapturedScreenshot",
    "DesktopAction",
    "DesktopActionResult",
    "DesktopBatchResult",
    "DesktopController",
    "DesktopElement",
    "DesktopInputBackend",
    "DesktopObservation",
    "DesktopPolicy",
    "DesktopStateSnapshot",
    "DesktopWindow",
    "ObservationSource",
    "PresenceDesktopHarness",
    "ScreenAwarenessMonitor",
    "ScreenCaptureBackend",
    "ScreenRect",
    "StateVerifier",
    "VisualElementCandidate",
    "VisualPerceptionResult",
    "VisionPerception",
    "WindowsManager",
]
