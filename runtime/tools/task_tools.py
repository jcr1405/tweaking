"""Native Task Management Tools
Provides the AgentExecutor and conversation sessions with authoritative control over
active tasks, background subprocesses, log inspection, and termination.
"""

import logging
from typing import Optional, Dict, Any, List

from runtime.permissions import PermissionTier
from runtime.tools.registry import ToolDefinition

logger = logging.getLogger("presence.tools.tasks")


def build_task_tools(
    task_registry: Any,
    process_manager: Optional[Any] = None
) -> List[ToolDefinition]:
    """Constructs ToolDefinition for presence.manage_task.
    Enables agents to list running tasks, inspect status/logs, deliver input, or kill tasks.
    """

    def _handle_manage_task(args: Dict[str, Any]) -> Dict[str, Any]:
        action = str(args.get("action", "list")).strip().lower()
        task_id = str(args.get("task_id", "")).strip() if args.get("task_id") else None
        input_text = str(args.get("input", "")) if args.get("input") is not None else None

        if action == "list":
            # 1. Fetch active tasks from registry
            active_nodes = task_registry.get_active_tasks() if hasattr(task_registry, "get_active_tasks") else []
            tasks_list = [n.to_dict() if hasattr(n, "to_dict") else dict(n) for n in active_nodes]

            # 2. Augment with background processes if process_manager is attached
            if process_manager and hasattr(process_manager, "list_processes"):
                proc_list = process_manager.list_processes(include_completed=False)
                for p in proc_list:
                    if not any(t.get("task_id") == p.get("task_id") for t in tasks_list):
                        tasks_list.append({
                            "task_id": p.get("task_id"),
                            "display_name": p.get("command", "")[:40],
                            "task_type": "SUBPROCESS",
                            "state": p.get("state"),
                            "elapsed_seconds": p.get("duration_seconds", 0.0),
                            "artifact_id": p.get("stdout_path"),
                            "metadata": {"pid": p.get("pid")}
                        })

            return {
                "status": "SUCCESS",
                "action": "list",
                "count": len(tasks_list),
                "tasks": tasks_list
            }

        elif action == "status":
            if not task_id:
                return {"status": "ERROR", "error": "Missing required 'task_id' for status query."}

            # Check process manager first for rich tail/PID metadata
            if process_manager and hasattr(process_manager, "get_process_status"):
                proc_st = process_manager.get_process_status(task_id)
                if proc_st:
                    return {
                        "status": "SUCCESS",
                        "action": "status",
                        "task_id": task_id,
                        "task_type": "SUBPROCESS",
                        "state": proc_st.get("state"),
                        "pid": proc_st.get("pid"),
                        "exit_code": proc_st.get("exit_code"),
                        "duration_seconds": proc_st.get("duration_seconds"),
                        "stdout_tail": proc_st.get("stdout_tail", ""),
                        "stderr_tail": proc_st.get("stderr_tail", ""),
                        "stdout_path": proc_st.get("stdout_path"),
                        "stderr_path": proc_st.get("stderr_path")
                    }

            # Check task registry
            node = task_registry.get_task(task_id) if hasattr(task_registry, "get_task") else None
            if node:
                return {
                    "status": "SUCCESS",
                    "action": "status",
                    "task": node.to_dict() if hasattr(node, "to_dict") else dict(node)
                }

            return {
                "status": "NOT_FOUND",
                "error": f"No active or recorded task found with task_id '{task_id}'."
            }

        elif action == "kill":
            if not task_id:
                return {"status": "ERROR", "error": "Missing required 'task_id' for kill operation."}

            killed = False
            # If process manager has it, terminate the Job Object container
            if process_manager and hasattr(process_manager, "kill_process"):
                killed = process_manager.kill_process(task_id, reason="Killed via presence.manage_task")

            # Also cancel via task registry
            if task_registry and hasattr(task_registry, "cancel_task"):
                reg_cancelled = task_registry.cancel_task(task_id, reason="Killed via presence.manage_task")
                killed = killed or reg_cancelled

            return {
                "status": "SUCCESS" if killed else "NOT_FOUND",
                "action": "kill",
                "task_id": task_id,
                "message": f"Task '{task_id}' successfully cancelled/terminated." if killed else f"Task '{task_id}' could not be terminated (not found or already terminal)."
            }

        elif action == "send_input":
            if not task_id:
                return {"status": "ERROR", "error": "Missing required 'task_id' for send_input."}
            if input_text is None:
                return {"status": "ERROR", "error": "Missing required 'input' payload for send_input."}

            if process_manager and hasattr(process_manager, "send_input"):
                delivered = process_manager.send_input(task_id, input_text)
                return {
                    "status": "SUCCESS" if delivered else "FAILED",
                    "action": "send_input",
                    "task_id": task_id,
                    "delivered": delivered,
                    "error": None if delivered else "Could not deliver input to process stdin."
                }

            return {
                "status": "ERROR",
                "error": "ProcessManager is not available to deliver stdin."
            }

        else:
            return {
                "status": "ERROR",
                "error": f"Unknown action '{action}'. Supported actions: 'list', 'status', 'kill', 'send_input'."
            }

    manage_task_tool = ToolDefinition(
        tool_id="presence.manage_task",
        name="presence.manage_task",
        description=(
            "Manage background tasks and supervised OS subprocesses. "
            "Actions: 'list' reports running tasks; 'status' inspects a task's state and recent log tail; "
            "'send_input' delivers stdin text to a running task; 'kill' terminates a task and its process tree."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "status", "kill", "send_input"],
                    "description": "The task management action to perform."
                },
                "task_id": {
                    "type": "string",
                    "description": "Unique task identifier (required for 'status', 'kill', 'send_input')."
                },
                "input": {
                    "type": "string",
                    "description": "Text input to deliver to the running task (required for 'send_input')."
                }
            },
            "required": ["action"]
        },
        authority_tier=PermissionTier.L0_OBSERVE,
        handler=_handle_manage_task
    )

    return [manage_task_tool]
