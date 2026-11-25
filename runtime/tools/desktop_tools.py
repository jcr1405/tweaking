"""
Presence Desktop Tools for Local Operations Supervisor.

Provides registered operational tools for discovering Windows applications,
inspecting UI Automation trees, running local multimodal vision perception,
executing grounded actions, and verifying desktop state changes.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from runtime.desktop.controller import DesktopController
from runtime.desktop.models import (
    ActionType,
    DesktopAction,
)
from runtime.permissions import PermissionTier
from runtime.tools.registry import ToolDefinition, ToolRegistry

logger = logging.getLogger("presence.tools.desktop")


def create_desktop_tools(controller: Optional[DesktopController] = None) -> List[ToolDefinition]:
    """
    Creates and returns tool definitions for desktop observation, perception, and interaction.
    """
    ctrl = controller or DesktopController()

    # 1. desktop.list_windows
    def _handle_list_windows(args: Dict[str, Any]) -> Dict[str, Any]:
        filter_vis = bool(args.get("filter_visible", True))
        windows = ctrl.list_windows(filter_visible=filter_vis)
        return {
            "status": "SUCCESS",
            "count": len(windows),
            "windows": [w.model_dump() for w in windows],
        }

    list_windows_tool = ToolDefinition(
        tool_id="desktop.list_windows",
        name="desktop.list_windows",
        description=(
            "Discover all running top-level Windows application windows on the desktop. "
            "Returns window IDs, titles, process names, bounds, and foreground state."
        ),
        parameters={
            "type": "object",
            "properties": {
                "filter_visible": {
                    "type": "boolean",
                    "description": "Whether to filter out invisible windows (default: true).",
                },
            },
        },
        authority_tier=PermissionTier.L0_OBSERVE,
        handler=_handle_list_windows,
        timeout_seconds=10.0,
    )

    # 2. desktop.observe
    def _handle_observe(args: Dict[str, Any]) -> Dict[str, Any]:
        window_id = str(args.get("window_id", "")).strip()
        if not window_id:
            return {"status": "ERROR", "error": "window_id parameter is required"}

        include_tree = bool(args.get("include_tree", True))
        include_screenshot = bool(args.get("include_screenshot", False))
        max_depth = int(args.get("max_depth", 5))
        max_nodes = int(args.get("max_nodes", 100))

        try:
            obs = ctrl.observe_window(
                window_id=window_id,
                include_tree=include_tree,
                include_screenshot=include_screenshot,
                max_depth=max_depth,
                max_nodes=max_nodes,
            )
            return {
                "status": "SUCCESS",
                "observation": obs.model_dump(),
            }
        except Exception as e:
            return {"status": "ERROR", "error": str(e)}

    observe_tool = ToolDefinition(
        tool_id="desktop.observe",
        name="desktop.observe",
        description=(
            "Inspect a target Windows application window's UI Automation accessibility tree "
            "and/or capture a visual snapshot. Accessibility-first: returns interactive elements "
            "(buttons, inputs, toggles) with ephemeral element IDs."
        ),
        parameters={
            "type": "object",
            "properties": {
                "window_id": {
                    "type": "string",
                    "description": "Ephemeral window identifier (e.g. win_<hwnd>_<pid>).",
                },
                "include_tree": {
                    "type": "boolean",
                    "description": "Whether to inspect accessibility elements (default: true).",
                },
                "include_screenshot": {
                    "type": "boolean",
                    "description": "Whether to capture an in-memory window screenshot (default: false).",
                },
                "max_depth": {
                    "type": "integer",
                    "description": "Maximum tree traversal depth (default: 5).",
                },
                "max_nodes": {
                    "type": "integer",
                    "description": "Maximum number of UI nodes to extract (default: 100).",
                },
            },
            "required": ["window_id"],
        },
        authority_tier=PermissionTier.L0_OBSERVE,
        handler=_handle_observe,
        timeout_seconds=15.0,
    )

    # 3. desktop.perceive
    def _handle_perceive(args: Dict[str, Any]) -> Dict[str, Any]:
        window_id = str(args.get("window_id", "")).strip()
        if not window_id:
            return {"status": "ERROR", "error": "window_id parameter is required"}

        query = args.get("query")
        task_id = args.get("task_id")

        try:
            res = ctrl.perceive_window(window_id=window_id, query=query, task_id=task_id)
            return {
                "status": "SUCCESS",
                "perception": res.model_dump(),
            }
        except Exception as e:
            return {"status": "ERROR", "error": str(e)}

    perceive_tool = ToolDefinition(
        tool_id="desktop.perceive",
        name="desktop.perceive",
        description=(
            "Run local Qwen 3.5 9B multimodal vision inference on a window screenshot "
            "to locate UI elements, buttons, or custom-drawn canvas items. "
            "Returns normalized bounding boxes and grounded physical desktop coordinates."
        ),
        parameters={
            "type": "object",
            "properties": {
                "window_id": {
                    "type": "string",
                    "description": "Target window identifier.",
                },
                "query": {
                    "type": "string",
                    "description": "Optional description of specific UI elements to locate.",
                },
            },
            "required": ["window_id"],
        },
        authority_tier=PermissionTier.L0_OBSERVE,
        handler=_handle_perceive,
        timeout_seconds=30.0,
    )

    # 4. desktop.execute_actions
    def _handle_execute_actions(args: Dict[str, Any]) -> Dict[str, Any]:
        window_id = str(args.get("window_id", "")).strip()
        if not window_id:
            return {"status": "ERROR", "error": "window_id parameter is required"}

        raw_actions = args.get("actions", [])
        if not isinstance(raw_actions, list) or not raw_actions:
            return {"status": "ERROR", "error": "actions parameter must be a non-empty list"}

        stop_on_failure = bool(args.get("stop_on_failure", True))

        parsed_actions: List[DesktopAction] = []
        for idx, act in enumerate(raw_actions):
            if not isinstance(act, dict):
                return {"status": "ERROR", "error": f"Action [{idx}] must be a dictionary"}
            try:
                # Ensure action_id exists
                if "action_id" not in act:
                    act["action_id"] = f"act_{idx + 1}"
                # Ensure action_type enum
                if isinstance(act.get("action_type"), str):
                    act["action_type"] = ActionType(act["action_type"])
                parsed_actions.append(DesktopAction(**act))
            except Exception as e:
                return {"status": "ERROR", "error": f"Invalid action specification at [{idx}]: {e}"}

        try:
            batch_result = ctrl.execute_batch(
                window_id=window_id,
                actions=parsed_actions,
                stop_on_failure=stop_on_failure,
            )
            return {
                "status": "SUCCESS" if batch_result.status == "completed" else "FAILED",
                "batch_result": batch_result.model_dump(),
            }
        except Exception as e:
            return {"status": "ERROR", "error": str(e)}

    execute_actions_tool = ToolDefinition(
        tool_id="desktop.execute_actions",
        name="desktop.execute_actions",
        description=(
            "Execute an ordered batch of desktop interactions on a target window. "
            "Accessibility-first: uses semantic UIA operations when element_id is provided, "
            "or grounded coordinate SendInput when coordinates are given. "
            "Verifies state transitions before and after execution."
        ),
        parameters={
            "type": "object",
            "properties": {
                "window_id": {
                    "type": "string",
                    "description": "Target window identifier.",
                },
                "actions": {
                    "type": "array",
                    "description": "Ordered list of DesktopAction specifications.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action_id": {"type": "string"},
                            "action_type": {
                                "type": "string",
                                "enum": [
                                    "invoke_element",
                                    "set_toggle",
                                    "set_value",
                                    "select_item",
                                    "expand_collapse",
                                    "scroll",
                                    "click_coordinate",
                                    "double_click",
                                    "right_click",
                                    "move_mouse",
                                    "drag",
                                    "type_text",
                                    "press_key",
                                    "press_shortcut",
                                    "wait_for_condition",
                                ],
                            },
                            "element_id": {"type": "string"},
                            "coordinate": {
                                "type": "array",
                                "items": {"type": "integer"},
                                "description": "[x, y] screen coordinates",
                            },
                            "text": {"type": "string"},
                            "desired_state": {"type": "boolean"},
                            "key": {"type": "string"},
                            "shortcut": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "scroll_direction": {"type": "string"},
                            "scroll_amount": {"type": "integer"},
                            "screenshot_id": {"type": "string"},
                            "expected_post_condition": {"type": "string"},
                        },
                        "required": ["action_type"],
                    },
                },
                "stop_on_failure": {
                    "type": "boolean",
                    "description": "Whether to halt the batch if an action fails or cannot be verified (default: true).",
                },
            },
            "required": ["window_id", "actions"],
        },
        authority_tier=PermissionTier.L2_EXECUTE_REVERSIBLE,
        handler=_handle_execute_actions,
        timeout_seconds=45.0,
    )

    # 5. desktop.verify
    def _handle_verify(args: Dict[str, Any]) -> Dict[str, Any]:
        window_id = str(args.get("window_id", "")).strip()
        if not window_id:
            return {"status": "ERROR", "error": "window_id parameter is required"}

        element_id = args.get("element_id")
        expected_condition = args.get("expected_condition")

        state = ctrl.verifier.capture_state(window_id=window_id, element_id=element_id)
        satisfied = True
        reason = None

        if expected_condition:
            dummy_act = DesktopAction(
                action_id="verify_probe",
                action_type=ActionType.WAIT_FOR_CONDITION,
                window_id=window_id,
                element_id=element_id,
                expected_post_condition=expected_condition,
            )
            satisfied, reason = ctrl.verifier.verify_action(
                action=dummy_act,
                state_before={},
                state_after=state,
            )

        return {
            "status": "SUCCESS",
            "state": state,
            "condition_satisfied": satisfied,
            "verification_note": reason,
        }

    verify_tool = ToolDefinition(
        tool_id="desktop.verify",
        name="desktop.verify",
        description=(
            "Explicitly query and verify the current state of a window or UI element, "
            "checking if an expected condition holds."
        ),
        parameters={
            "type": "object",
            "properties": {
                "window_id": {
                    "type": "string",
                    "description": "Target window identifier.",
                },
                "element_id": {
                    "type": "string",
                    "description": "Optional element identifier to inspect.",
                },
                "expected_condition": {
                    "type": "string",
                    "description": "Condition to verify (e.g. 'window_closed', 'foreground', 'value_changed').",
                },
            },
            "required": ["window_id"],
        },
        authority_tier=PermissionTier.L0_OBSERVE,
        handler=_handle_verify,
        timeout_seconds=10.0,
    )

    return [
        list_windows_tool,
        observe_tool,
        perceive_tool,
        execute_actions_tool,
        verify_tool,
    ]


def register_desktop_tools(
    registry: ToolRegistry,
    controller: Optional[DesktopController] = None,
) -> None:
    """Registers all desktop tools with the provided ToolRegistry."""
    tools = create_desktop_tools(controller)
    for tool in tools:
        registry.register(tool)
