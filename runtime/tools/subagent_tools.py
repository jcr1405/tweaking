"""Native Subagent Execution and Coordination Tools
Exposes presence.invoke_subagent, presence.send_subagent_message, and presence.manage_subagents
to the AgentExecutor under governed permission boundaries.
"""

import logging
from typing import Optional, Dict, Any, List

from runtime.permissions import PermissionTier
from runtime.tools.registry import ToolDefinition
from runtime.subagents.manager import SubagentManager
from runtime.subagents.models import SubagentConfig, SubagentRole

logger = logging.getLogger("presence.tools.subagents")


def build_subagent_tools(
    subagent_manager: SubagentManager,
    current_conversation_id: str,
    pin_local_ollama: bool = False,
    max_parent_subagents: Optional[int] = None,
) -> List[ToolDefinition]:
    """Constructs ToolDefinitions for subagent invocation, messaging, and management."""

    def _handle_invoke_subagent(args: Dict[str, Any]) -> Dict[str, Any]:
        prompt = args.get("prompt", "")
        role = args.get("role", SubagentRole.GENERAL.value)
        tools = args.get("tools")
        wait_ms = int(args.get("wait_ms", 10000))
        timeout_seconds = float(args.get("timeout_seconds", 300.0))

        config = SubagentConfig(
            role=role,
            prompt=prompt,
            tools_whitelist=tools,
            timeout_seconds=timeout_seconds,
            pin_local_ollama=pin_local_ollama,
            max_parent_subagents=max_parent_subagents,
        )

        return subagent_manager.spawn_subagent(
            parent_conversation_id=current_conversation_id,
            config=config,
            wait_ms=wait_ms
        )

    def _handle_send_subagent_message(args: Dict[str, Any]) -> Dict[str, Any]:
        subagent_id = args.get("subagent_id", "")
        message = args.get("message", "")
        if not subagent_id or not message:
            return {"status": "ERROR", "error": "subagent_id and message are required."}

        return subagent_manager.send_message(subagent_id=subagent_id, message=message)

    def _handle_manage_subagents(args: Dict[str, Any]) -> Dict[str, Any]:
        action = args.get("action", "list").lower()
        subagent_id = args.get("subagent_id")

        if action == "list":
            subagents = subagent_manager.list_subagents(
                parent_conversation_id=current_conversation_id,
                include_completed=True
            )
            return {"status": "SUCCESS", "subagents": subagents, "count": len(subagents)}

        elif action == "status":
            if not subagent_id:
                return {"status": "ERROR", "error": "subagent_id is required for 'status' action."}
            status = subagent_manager.get_subagent_status(subagent_id)
            if not status:
                return {"status": "ERROR", "error": f"Subagent '{subagent_id}' not found."}
            return {"status": "SUCCESS", "subagent": status}

        elif action == "kill":
            if not subagent_id:
                return {"status": "ERROR", "error": "subagent_id is required for 'kill' action."}
            killed = subagent_manager.kill_subagent(subagent_id, reason="Terminated via manage_subagents tool")
            return {"status": "SUCCESS" if killed else "FAILED", "subagent_id": subagent_id, "killed": killed}

        elif action == "kill_all":
            count = subagent_manager.kill_all(parent_conversation_id=current_conversation_id)
            return {"status": "SUCCESS", "killed_count": count}

        return {"status": "ERROR", "error": f"Unknown action '{action}'. Valid actions: list, status, kill, kill_all."}

    invoke_subagent_tool = ToolDefinition(
        tool_id="presence.invoke_subagent",
        name="invoke_subagent",
        description=(
            "Spawns an isolated subagent to perform a compartmentalized, multi-step task "
            "(such as broad research, multi-query web lookups, code analysis, or verification) "
            "without cluttering the main conversation history. Returns a distilled summary receipt."
        ),
        parameters={
            "type": "object",
            "properties": {
                "role": {
                    "type": "string",
                    "description": "Role profile for the subagent ('research', 'code_analysis', 'verifier', 'general').",
                    "enum": ["research", "code_analysis", "verifier", "general"]
                },
                "prompt": {
                    "type": "string",
                    "description": "Clear, detailed, actionable instructions for what the subagent should do and return."
                },
                "tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional explicit whitelist of tool names to grant to the subagent."
                },
                "wait_ms": {
                    "type": "integer",
                    "description": "Milliseconds to wait synchronously before backgrounding (default 10000ms)."
                },
                "timeout_seconds": {
                    "type": "number",
                    "description": "Maximum execution duration before cancelling the subagent (default 300s)."
                }
            },
            "required": ["prompt"],
            "additionalProperties": False
        },
        authority_tier=PermissionTier.L2_EXECUTE_REVERSIBLE,
        handler=_handle_invoke_subagent,
        read_only=False,
    )

    send_subagent_message_tool = ToolDefinition(
        tool_id="presence.send_subagent_message",
        name="send_subagent_message",
        description="Delivers an additional user message or guidance to a running subagent.",
        parameters={
            "type": "object",
            "properties": {
                "subagent_id": {
                    "type": "string",
                    "description": "The unique ID of the target subagent."
                },
                "message": {
                    "type": "string",
                    "description": "Content of the follow-up message to deliver."
                }
            },
            "required": ["subagent_id", "message"],
            "additionalProperties": False
        },
        authority_tier=PermissionTier.L2_EXECUTE_REVERSIBLE,
        handler=_handle_send_subagent_message,
        read_only=False,
    )

    manage_subagents_tool = ToolDefinition(
        tool_id="presence.manage_subagents",
        name="manage_subagents",
        description="Manages active subagents: lists subagents, checks status, or terminates running subagents.",
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Action to perform: 'list', 'status', 'kill', or 'kill_all'.",
                    "enum": ["list", "status", "kill", "kill_all"]
                },
                "subagent_id": {
                    "type": "string",
                    "description": "Target subagent ID (required for 'status' and 'kill')."
                }
            },
            "required": ["action"],
            "additionalProperties": False
        },
        authority_tier=PermissionTier.L0_OBSERVE,
        handler=_handle_manage_subagents,
        read_only=True,
    )

    return [invoke_subagent_tool, send_subagent_message_tool, manage_subagents_tool]
