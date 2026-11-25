"""
Presence Desktop Controller.

Unified orchestrator for Windows desktop interaction, accessibility automation,
vision-guided perception, and input execution.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from runtime.desktop.accessibility import AccessibilityBackend
from runtime.desktop.capture import CapturedScreenshot, ScreenCaptureBackend
from runtime.desktop.input import DesktopInputBackend
from runtime.desktop.fusion import FastVisualGrounder
from runtime.desktop.models import (
    ActionStatus,
    ActionType,
    DesktopAction,
    DesktopActionResult,
    DesktopBatchResult,
    DesktopObservation,
    DesktopWindow,
    GroundedCandidate,
    GroundingResolution,
    ObservationSource,
    PerceptionRoute,
    ScreenRect,
    VisualPerceptionResult,
)
from runtime.desktop.perception import VisionPerception
from runtime.desktop.policy import DesktopPolicy
from runtime.desktop.verification import StateVerifier
from runtime.desktop.windows import WindowsManager

logger = logging.getLogger(__name__)


class DesktopController:
    """
    Central controller managing Windows desktop observation, interaction,
    safety gating, and verification.
    """

    def __init__(
        self,
        windows_manager: Optional[WindowsManager] = None,
        accessibility_backend: Optional[AccessibilityBackend] = None,
        capture_backend: Optional[ScreenCaptureBackend] = None,
        perception: Optional[VisionPerception] = None,
        input_backend: Optional[DesktopInputBackend] = None,
        policy: Optional[DesktopPolicy] = None,
        verifier: Optional[StateVerifier] = None,
        fast_grounder: Optional[FastVisualGrounder] = None,
        dry_run: bool = False,
    ):
        self.windows_manager = windows_manager or WindowsManager()
        self.accessibility_backend = accessibility_backend or AccessibilityBackend()
        self.capture_backend = capture_backend or ScreenCaptureBackend(self.windows_manager)
        self.perception = perception or VisionPerception()
        self.input_backend = input_backend or DesktopInputBackend(
            windows_manager=self.windows_manager,
            dry_run=dry_run,
        )
        self.policy = policy or DesktopPolicy()
        self.verifier = verifier or StateVerifier(
            windows_manager=self.windows_manager,
            accessibility_backend=self.accessibility_backend,
        )
        self.fast_grounder = fast_grounder or FastVisualGrounder()

    def list_windows(self, filter_visible: bool = True) -> List[DesktopWindow]:
        """Discover all running top-level Windows application windows."""
        return self.windows_manager.list_windows(filter_visible=filter_visible)

    def get_window(self, window_id: str) -> Optional[DesktopWindow]:
        """Get window metadata by window_id."""
        return self.windows_manager.get_window(window_id)

    def find_window_by_title(self, pattern: str) -> Optional[DesktopWindow]:
        """Find the first window whose title matches a pattern."""
        return self.windows_manager.find_window_by_title(pattern)

    def observe_window(
        self,
        window_id: str,
        include_tree: bool = True,
        include_screenshot: bool = False,
        max_depth: int = 5,
        max_nodes: int = 100,
    ) -> DesktopObservation:
        """
        Observes a target window's accessibility hierarchy and/or visual appearance.
        Accessibility-first: provides rich semantic controls when available.
        """
        win = self.windows_manager.get_window(window_id)
        if not win:
            raise ValueError(f"Window with id '{window_id}' not found")

        obs: Optional[DesktopObservation] = None
        if include_tree and win.hwnd:
            try:
                obs = self.accessibility_backend.inspect_window(win)
            except Exception as err:
                logger.warning(f"Failed to inspect accessibility tree for {window_id}: {err}")

        if not obs:
            obs = DesktopObservation(
                observation_id=f"obs_{uuid.uuid4().hex[:8]}",
                window_id=window_id,
                observed_at=datetime.now(timezone.utc).isoformat(),
                source=ObservationSource.ACCESSIBILITY,
                window=win,
                elements=[],
                screenshot_id=None,
                truncated=False,
                total_nodes_found=0,
            )

        if include_screenshot:
            shot = self.capture_backend.capture_window(window_id)
            if shot:
                obs.screenshot_id = shot.screenshot_id
                obs.source = (
                    ObservationSource.HYBRID
                    if (include_tree and obs.elements)
                    else ObservationSource.VISION
                )

        return obs

    def capture_screenshot(self, window_id: Optional[str] = None) -> Optional[CapturedScreenshot]:
        """Captures a screenshot of a specific window or entire desktop."""
        if window_id:
            return self.capture_backend.capture_window(window_id)
        return self.capture_backend.capture_screen()

    def perceive_window(
        self,
        window_id: str,
        query: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> VisualPerceptionResult:
        """
        Captures a window screenshot and runs local Qwen multimodal vision inference
        to locate elements visually.
        """
        shot = self.capture_backend.capture_window(window_id)
        if not shot:
            win = self.windows_manager.get_window(window_id)
            return VisualPerceptionResult(
                screenshot_id="missing",
                window_id=window_id,
                image_width=win.bounds.width if win else 0,
                image_height=win.bounds.height if win else 0,
                observations=[],
                overall_ambiguity=f"Failed to capture screenshot for window {window_id}",
            )

        return self.perception.analyze_screenshot(shot, query=query, task_id=task_id)

    def ground_control(
        self,
        window_id: str,
        query: str,
        force_route: Optional[PerceptionRoute] = None,
        task_id: Optional[str] = None,
    ) -> GroundingResolution:
        """
        Intelligently resolves a target query against the desktop using the 3-Tier Cascade:
        Tier 1: Windows UI Automation (Semantic UIA element)
        Tier 2: Fast Visual Grounding (Native OCR + Lightweight UI Detector)
        Tier 3: Qwen 3.5 9B Multimodal Vision Reasoning (Fallback for ambiguity / complex scenes)
        """
        win = self.windows_manager.get_window(window_id)
        if not win:
            return GroundingResolution(
                target_query=query,
                selected_candidate=None,
                all_candidates=[],
                route=PerceptionRoute.AMBIGUOUS,
                is_ambiguous=True,
                ambiguity_reason=f"Target window {window_id} not found",
                confidence=0.0,
            )

        q_clean = query.strip().lower()

        # -------------------------------------------------------------
        # TIER 1: Windows UI Automation (Semantic Fast Path)
        # -------------------------------------------------------------
        if force_route is None or force_route == PerceptionRoute.UIA:
            try:
                obs = self.accessibility_backend.inspect_window(win)
                for elem in obs.elements:
                    e_name = (elem.name or "").strip().lower()
                    if e_name and (q_clean == e_name or q_clean in e_name or e_name in q_clean):
                        # Accessible control found!
                        desk_coord = None
                        if elem.bounds:
                            desk_coord = elem.bounds.center()

                        candidate = GroundedCandidate(
                            candidate_id=f"gc_{elem.element_id}",
                            label=elem.name,
                            category=elem.control_type.lower(),
                            bbox_normalized=[0.0, 0.0, 0.0, 0.0],
                            confidence=1.0,
                            grounding_source=["uia"],
                            pixel_rect=elem.bounds,
                            desktop_coordinate=desk_coord,
                            is_ambiguous=False,
                            actionable=elem.enabled,
                        )
                        return GroundingResolution(
                            target_query=query,
                            selected_candidate=candidate,
                            all_candidates=[candidate],
                            route=PerceptionRoute.UIA,
                            is_ambiguous=False,
                            ambiguity_reason=None,
                            confidence=1.0,
                        )
            except Exception as uia_err:
                logger.debug(f"Tier 1 UIA check bypassed: {uia_err}")

        # -------------------------------------------------------------
        # TIER 2: Fast Visual Grounding (Native OCR + UI Detector)
        # -------------------------------------------------------------
        if force_route is None or force_route in (
            PerceptionRoute.OCR_DETECTOR,
            PerceptionRoute.OCR,
            PerceptionRoute.DETECTOR,
        ):
            shot = self.capture_backend.capture_window(window_id)
            if shot:
                res_tier2 = self.fast_grounder.ground_target(
                    image_bytes=shot.image_bytes,
                    query=query,
                    screenshot_id=shot.screenshot_id,
                    window_bounds=win.bounds,
                )
                # If unambiguous match found, return Tier 2 fast path result!
                if not res_tier2.is_ambiguous and res_tier2.route != PerceptionRoute.QWEN_VISION:
                    return res_tier2

                # If force_route was explicitly OCR/Detector, return even if ambiguous
                if force_route in (PerceptionRoute.OCR_DETECTOR, PerceptionRoute.OCR, PerceptionRoute.DETECTOR):
                    return res_tier2

        # -------------------------------------------------------------
        # TIER 3: Qwen 3.5 9B Multimodal Vision Reasoning (Fallback)
        # -------------------------------------------------------------
        v_res = self.perceive_window(window_id, query=query, task_id=task_id)
        best_cand: Optional[GroundedCandidate] = None
        best_score = 0.0

        for obs_item in v_res.observations:
            obs_label = obs_item.label.lower()
            score = 0.0
            if q_clean == obs_label:
                score = 1.0
            elif q_clean in obs_label or obs_label in q_clean:
                score = 0.85
            else:
                score = 0.60

            if score > best_score:
                best_score = score
                abs_x, abs_y = self.perception.ground_to_desktop_coordinates(obs_item, win.bounds)
                best_cand = GroundedCandidate(
                    candidate_id=f"gc_{uuid.uuid4().hex[:8]}",
                    label=obs_item.label,
                    category=obs_item.role or "control",
                    bbox_normalized=obs_item.bbox_normalized,
                    confidence=obs_item.confidence,
                    grounding_source=["qwen_vision"],
                    pixel_rect=None,
                    desktop_coordinate=(abs_x, abs_y),
                    is_ambiguous=bool(v_res.overall_ambiguity),
                    ambiguity_reason=v_res.overall_ambiguity,
                    actionable=obs_item.actionable,
                )

        return GroundingResolution(
            target_query=query,
            selected_candidate=best_cand,
            all_candidates=[best_cand] if best_cand else [],
            route=PerceptionRoute.QWEN_VISION,
            is_ambiguous=bool(v_res.overall_ambiguity) or (best_cand is None),
            ambiguity_reason=v_res.overall_ambiguity or ("No visual match found" if not best_cand else None),
            confidence=best_score if best_cand else 0.0,
        )

    def execute_action(self, action: DesktopAction) -> DesktopActionResult:
        """
        Executes a single desktop action following accessibility-first routing,
        safety policy evaluation, and post-action state verification.
        """
        started_at = datetime.now(timezone.utc).isoformat()
        target_win = None
        if action.window_id:
            # Re-verify window identity to detect recycled HWND or closed window
            if hasattr(self.windows_manager, "validate_window_identity"):
                val_res = self.windows_manager.validate_window_identity(action.window_id)
                if isinstance(val_res, tuple) and len(val_res) == 2:
                    is_valid, val_err = val_res
                    if not is_valid:
                        return DesktopActionResult(
                            action_id=action.action_id,
                            status=ActionStatus.FAILED,
                            target_window_id=action.window_id,
                            target_element_id=action.element_id,
                            action_type=action.action_type.value,
                            started_at=started_at,
                            completed_at=datetime.now(timezone.utc).isoformat(),
                            verified=False,
                            failure_reason=f"Target window validation failed: {val_err}",
                        )
            target_win = self.windows_manager.get_window(action.window_id)

        # Retrieve element metadata if element_id is specified
        element_metadata = None
        if action.element_id and hasattr(self.accessibility_backend, "get_registered_element"):
            try:
                element_metadata = self.accessibility_backend.get_registered_element(action.element_id)
            except Exception:
                pass

        # 1. Policy check
        allowed, reason = self.policy.evaluate_action(
            action=action,
            target_window=target_win,
            element_metadata=element_metadata,
        )
        if not allowed:
            return DesktopActionResult(
                action_id=action.action_id,
                status=ActionStatus.BLOCKED,
                target_window_id=action.window_id,
                target_element_id=action.element_id,
                action_type=action.action_type.value,
                started_at=started_at,
                completed_at=datetime.now(timezone.utc).isoformat(),
                verified=False,
                failure_reason=f"Blocked by safety policy: {reason}",
            )

        # 2. Capture pre-action state
        observed_before = self.verifier.capture_state(
            window_id=action.window_id,
            element_id=action.element_id,
        )

        # 3. Routing: Accessibility-first semantic execution vs. Coordinate input
        success = False
        exec_error = None

        # Check if semantic UIA execution
        is_semantic_uia = action.element_id is not None and action.action_type in (
            ActionType.INVOKE_ELEMENT,
            ActionType.SET_TOGGLE,
            ActionType.SET_VALUE,
            ActionType.SELECT_ITEM,
            ActionType.EXPAND_COLLAPSE,
        )

        if is_semantic_uia:
            assert action.element_id is not None
            try:
                if action.action_type == ActionType.INVOKE_ELEMENT:
                    self.accessibility_backend.invoke_element(action.element_id)
                elif action.action_type == ActionType.SET_TOGGLE:
                    self.accessibility_backend.set_toggle(
                        action.element_id, action.desired_state if action.desired_state is not None else True
                    )
                elif action.action_type == ActionType.SET_VALUE:
                    self.accessibility_backend.set_value(
                        action.element_id, action.text or ""
                    )
                elif action.action_type == ActionType.SELECT_ITEM:
                    self.accessibility_backend.select_item(action.element_id)
                elif action.action_type == ActionType.EXPAND_COLLAPSE:
                    self.accessibility_backend.expand_collapse(
                        action.element_id, action.desired_state if action.desired_state is not None else True
                    )
                success = True
            except Exception as e:
                success = False
                exec_error = str(e)
        else:
            # Physical / SendInput execution
            evidence_ts = None
            if action.screenshot_id:
                shot = self.capture_backend.get_screenshot(action.screenshot_id)
                if shot:
                    evidence_ts = shot.captured_at_timestamp

            input_res = self.input_backend.execute_action(
                action=action,
                evidence_timestamp=evidence_ts,
                max_age_seconds=10.0,
            )
            success = input_res.status == ActionStatus.COMPLETED
            exec_error = input_res.failure_reason

        # 4. Capture post-action state
        # Short settle delay
        time.sleep(0.05)
        observed_after = self.verifier.capture_state(
            window_id=action.window_id,
            element_id=action.element_id,
        )

        # 5. Verification
        verified = False
        verif_reason = None
        if success:
            verified, verif_reason = self.verifier.verify_action(
                action=action,
                state_before=observed_before,
                state_after=observed_after,
            )
            if not verified:
                exec_error = verif_reason

        status = ActionStatus.COMPLETED if (success and verified) else ActionStatus.FAILED

        return DesktopActionResult(
            action_id=action.action_id,
            status=status,
            target_window_id=action.window_id,
            target_element_id=action.element_id,
            action_type=action.action_type.value,
            started_at=started_at,
            completed_at=datetime.now(timezone.utc).isoformat(),
            observed_before=observed_before,
            observed_after=observed_after,
            verified=verified,
            failure_reason=exec_error,
        )

    def execute_batch(
        self,
        window_id: str,
        actions: List[DesktopAction],
        stop_on_failure: bool = True,
    ) -> DesktopBatchResult:
        """
        Executes an ordered batch of desktop actions on a target window.
        """
        batch_id = f"batch_{uuid.uuid4().hex[:8]}"
        target_win = self.windows_manager.get_window(window_id)

        # 1. Batch policy check
        elem_fn = getattr(self.accessibility_backend, "get_registered_element", None)
        allowed, reason = self.policy.evaluate_batch(
            actions=actions,
            target_window=target_win,
            get_element_meta_fn=elem_fn,
        )
        if not allowed:
            return DesktopBatchResult(
                batch_id=batch_id,
                window_id=window_id,
                status=ActionStatus.BLOCKED,
                total_actions=len(actions),
                completed_actions=0,
                action_results=[],
                stopped_on_failure=True,
                summary=f"Batch blocked by safety policy: {reason}",
            )

        # Bring window to foreground before starting batch
        self.windows_manager.set_foreground(window_id)

        action_results: List[DesktopActionResult] = []
        stopped_on_failure_flag = False

        for action in actions:
            # Inherit window_id if not explicitly set
            if not action.window_id:
                action = action.model_copy(update={"window_id": window_id})

            res = self.execute_action(action)
            action_results.append(res)

            if res.status != ActionStatus.COMPLETED:
                if stop_on_failure:
                    stopped_on_failure_flag = True
                    break

        completed_count = sum(1 for r in action_results if r.status == ActionStatus.COMPLETED)
        final_status = (
            ActionStatus.COMPLETED
            if completed_count == len(actions)
            else (ActionStatus.FAILED if stopped_on_failure_flag else ActionStatus.CANCELLED)
        )

        return DesktopBatchResult(
            batch_id=batch_id,
            window_id=window_id,
            status=final_status,
            total_actions=len(actions),
            completed_actions=completed_count,
            action_results=action_results,
            stopped_on_failure=stopped_on_failure_flag,
            summary=f"Batch {batch_id}: {completed_count}/{len(actions)} actions completed successfully.",
        )
