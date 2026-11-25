"""Desktop Workflow Adapter
Executes desktop observation, fast visual grounding, UIA interaction,
and policy-gated desktop operations via DesktopController.
Enforces 3-tier grounding cascade, protected window isolation, and single-use consent gating.
"""

import logging
import uuid
from typing import Dict, Any, Optional, List, Tuple

from runtime.workflows.models import WorkflowNode, NodeState
from runtime.workflows.adapters.base import BaseWorkflowAdapter, NodeExecutionContext, NodeExecutionResult
from runtime.desktop.models import (
    ActionType,
    DesktopAction,
    ActionStatus,
    PerceptionRoute,
)

logger = logging.getLogger("presence.workflows.adapters.desktop")


class DesktopWorkflowAdapter(BaseWorkflowAdapter):
    """Adapter executing DESKTOP_OPERATION nodes through DesktopController."""

    def execute(self, node: WorkflowNode, context: NodeExecutionContext) -> NodeExecutionResult:
        controller = context.desktop_controller
        if not controller:
            return NodeExecutionResult(
                state=NodeState.FAILED,
                error_message="DesktopController is required in NodeExecutionContext for DESKTOP_OPERATION node.",
            )

        if context.cancellation_token and context.cancellation_token.is_cancelled:
            return NodeExecutionResult(
                state=NodeState.CANCELLED,
                error_message="Execution cancelled before node started.",
            )

        payload = node.payload or {}

        # 1. Resolve template variables with strict dependency authorization
        window_id = payload.get("window_id")
        if isinstance(window_id, str):
            window_id = context.resolve_template_variables(window_id, authorized_dependencies=node.depends_on)

        window_title_pattern = payload.get("window_title_pattern")
        if isinstance(window_title_pattern, str):
            window_title_pattern = context.resolve_template_variables(window_title_pattern, authorized_dependencies=node.depends_on)

        query = payload.get("query")
        if isinstance(query, str):
            query = context.resolve_template_variables(query, authorized_dependencies=node.depends_on)

        text = payload.get("text")
        if isinstance(text, str):
            text = context.resolve_template_variables(text, authorized_dependencies=node.depends_on)

        consent_token = payload.get("consent_token")
        if isinstance(consent_token, str):
            consent_token = context.resolve_template_variables(consent_token, authorized_dependencies=node.depends_on)

        element_id = payload.get("element_id")
        coordinate = payload.get("coordinate")
        action_type_raw = str(payload.get("action_type", "observe")).lower()
        force_route_raw = payload.get("force_route")

        # 2. Predecessor window resolution fallback
        if not window_id and not window_title_pattern and node.depends_on:
            pred_results = context.get_predecessor_results(node.depends_on)
            for dep_id, dep_data in pred_results.items():
                p_load = dep_data.get("result_payload", {})
                if isinstance(p_load, dict):
                    if "target_window_id" in p_load and p_load["target_window_id"]:
                        window_id = p_load["target_window_id"]
                        break
                    elif "window_id" in p_load and p_load["window_id"]:
                        window_id = p_load["window_id"]
                        break

        # 3. Discover window by title pattern if window_id not provided
        if not window_id and window_title_pattern:
            found_win = controller.find_window_by_title(window_title_pattern)
            if found_win:
                window_id = found_win.window_id
            else:
                return NodeExecutionResult(
                    state=NodeState.FAILED,
                    error_message=f"No window matching pattern '{window_title_pattern}' found.",
                )

        # 4. Handle pure observation action
        if action_type_raw in ("observe", "inspect"):
            if not window_id:
                # Discover visible windows
                windows = controller.list_windows(filter_visible=True)
                return NodeExecutionResult(
                    state=NodeState.COMPLETED,
                    result_payload={"windows": [w.model_dump() for w in windows], "count": len(windows)},
                )
            include_screenshot = bool(payload.get("include_screenshot", False))
            obs = controller.observe_window(
                window_id=window_id,
                include_tree=True,
                include_screenshot=include_screenshot,
            )
            return NodeExecutionResult(
                state=NodeState.COMPLETED,
                result_payload=obs.model_dump(),
            )

        # 5. Handle Visual/UIA Grounding via 3-Tier Cascade
        force_route = None
        if force_route_raw:
            try:
                force_route = PerceptionRoute(force_route_raw)
            except ValueError:
                pass

        grounding_resolution = None
        if query and not coordinate and not element_id:
            if not window_id:
                return NodeExecutionResult(
                    state=NodeState.FAILED,
                    error_message=f"Target window_id is required to ground query '{query}'.",
                )

            grounding_resolution = controller.ground_control(
                window_id=window_id,
                query=query,
                force_route=force_route,
                task_id=node.node_id,
            )

            if not grounding_resolution.selected_candidate:
                return NodeExecutionResult(
                    state=NodeState.FAILED,
                    error_message=(
                        f"Visual grounding failed for target query '{query}'. "
                        f"Reason: {grounding_resolution.ambiguity_reason or 'No candidate matched query'}."
                    ),
                    result_payload={"grounding_resolution": grounding_resolution.model_dump()},
                )

            cand = grounding_resolution.selected_candidate
            coordinate = cand.desktop_coordinate

            # If action was ground_and_click or unspecified, default to click_coordinate
            if action_type_raw in ("ground_and_click", "click", "observe"):
                action_type_raw = "click_coordinate"

        # 6. Parse ActionType enum
        try:
            action_type_enum = ActionType(action_type_raw)
        except ValueError:
            # Map common aliases
            if action_type_raw in ("click", "click_coordinate", "ground_and_click"):
                action_type_enum = ActionType.CLICK_COORDINATE if coordinate else ActionType.INVOKE_ELEMENT
            elif action_type_raw in ("type", "type_text"):
                action_type_enum = ActionType.TYPE_TEXT
            elif action_type_raw in ("wait", "wait_for_condition"):
                action_type_enum = ActionType.WAIT_FOR_CONDITION
            elif action_type_raw in ("invoke", "invoke_element"):
                action_type_enum = ActionType.INVOKE_ELEMENT
            else:
                return NodeExecutionResult(
                    state=NodeState.FAILED,
                    error_message=f"Unsupported desktop action_type '{action_type_raw}'.",
                )

        # 7. Construct DesktopAction
        target_elem_name = payload.get("target_element_name") or (
            grounding_resolution.selected_candidate.label if grounding_resolution and grounding_resolution.selected_candidate else None
        )

        coord_tuple = None
        if coordinate and isinstance(coordinate, (list, tuple)) and len(coordinate) >= 2:
            coord_tuple = (int(coordinate[0]), int(coordinate[1]))

        action_id_str = payload.get("action_id") or f"act_{node.node_id}"
        action = DesktopAction(
            action_id=action_id_str,
            action_type=action_type_enum,
            window_id=window_id,
            element_id=element_id,
            target_element_name=target_elem_name,
            coordinate=coord_tuple,
            text=text,
            consent_token=consent_token,
            task_id=node.node_id,
            conversation_id=context.conversation_id,
        )

        # 8. Action-Level Safety Policy Evaluation & Consent Gating
        if hasattr(controller, "policy") and controller.policy:
            if not controller.policy.permission_boundary and context.permission_boundary:
                controller.policy.permission_boundary = context.permission_boundary

        target_win = None
        if window_id and hasattr(controller, "windows_manager") and controller.windows_manager:
            try:
                target_win = controller.windows_manager.get_window(window_id)
            except Exception:
                pass
        elif window_id and hasattr(controller, "find_window_by_title"):
            try:
                target_win = controller.find_window_by_title(window_id)
            except Exception:
                pass
        element_meta = None
        if element_id and hasattr(controller.accessibility_backend, "get_registered_element"):
            try:
                element_meta = controller.accessibility_backend.get_registered_element(element_id)
            except Exception:
                pass

        if not consent_token:
            allowed, reason = controller.policy.evaluate_action(
                action=action,
                target_window=target_win,
                element_metadata=element_meta,
            )

            if not allowed:
                # Check if this requires user confirmation (Component I: WAITING_FOR_APPROVAL)
                reason_str = reason or "Action blocked by desktop safety policy"
                is_consent_required = any(
                    kw in reason_str.lower()
                    for kw in ("requires explicit user confirmation", "dangerous", "destructive", "consequential", "blocked")
                )

                if is_consent_required:
                    logger.warning(f"DESKTOP_OPERATION '{node.node_id}' entered WAITING_FOR_APPROVAL: {reason_str}")
                    return NodeExecutionResult(
                        state=NodeState.WAITING_FOR_APPROVAL,
                        error_message=reason_str,
                        result_payload={
                            "action": action.model_dump(),
                            "requires_consent": True,
                            "blocked_reason": reason_str,
                            "grounding_resolution": grounding_resolution.model_dump() if grounding_resolution else None,
                        },
                    )
                else:
                    logger.error(f"DESKTOP_OPERATION '{node.node_id}' BLOCKED: {reason_str}")
                    return NodeExecutionResult(
                        state=NodeState.FAILED,
                        error_message=f"Desktop policy rejected action: {reason_str}",
                        result_payload={
                            "action": action.model_dump(),
                            "blocked_reason": reason_str,
                        },
                    )

        # 9. Execute action via DesktopController
        act_result = controller.execute_action(action)

        res_dict = act_result.model_dump()
        if grounding_resolution:
            res_dict["grounding_resolution"] = grounding_resolution.model_dump()

        # 10. Post-action state verification if requested
        verify_assertion = payload.get("verify_assertion") or payload.get("expected_post_condition")
        if verify_assertion and hasattr(controller, "verify_state"):
            verified_ok = controller.verify_state(
                assertion_type=payload.get("assertion_type", "state_transition"),
                expected_value=verify_assertion,
                window_id=window_id,
            )
            res_dict["post_verified"] = verified_ok
            if not verified_ok:
                return NodeExecutionResult(
                    state=NodeState.FAILED,
                    error_message=f"Post-action state verification failed for condition: '{verify_assertion}'",
                    result_payload=res_dict,
                )

        if act_result.status == ActionStatus.COMPLETED:
            return NodeExecutionResult(
                state=NodeState.COMPLETED,
                result_payload=res_dict,
            )
        else:
            return NodeExecutionResult(
                state=NodeState.FAILED,
                error_message=act_result.failure_reason or f"Desktop action failed with status {act_result.status.value}",
                result_payload=res_dict,
            )
