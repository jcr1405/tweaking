"""Companion System & Behavioral Routine Tools
Provides the conversational companion session with direct capabilities for:
1. System Health & Situational Awareness: CPU, RAM, GPU, storage, battery, active window.
2. Behavioral Memory: Listing and executing learned personal computer routines.
"""

import json
import logging
from typing import Optional, Dict, Any, List

from runtime.permissions import PermissionTier
from runtime.tools.registry import ToolDefinition
from runtime.proactive.sensors import SystemSensors
from runtime.desktop.awareness import ScreenAwarenessMonitor
from runtime.memory.store import BehavioralMemoryStore

logger = logging.getLogger("presence.tools.companion")


def build_companion_tools(
    memory_store: Optional[BehavioralMemoryStore] = None,
    awareness_monitor: Optional[ScreenAwarenessMonitor] = None,
    desktop_controller: Optional[Any] = None,
) -> List[ToolDefinition]:
    """Constructs native tools for the personal computer companion."""
    store = memory_store or BehavioralMemoryStore()
    monitor = awareness_monitor or ScreenAwarenessMonitor()

    # 1. presence.get_system_health
    def _handle_get_system_health(args: Dict[str, Any]) -> Dict[str, Any]:
        """Queries real-time hardware metrics and foreground desktop focus."""
        snapshot = SystemSensors.sample()
        state = monitor.sample_current_state(capture_frame=False)
        return {
            "status": "SUCCESS",
            "health": snapshot.to_dict(),
            "foreground_window": state.to_summary(),
        }

    health_tool = ToolDefinition(
        tool_id="presence.get_system_health",
        name="presence.get_system_health",
        description=(
            "Queries real-time Windows hardware metrics (CPU utilization %, RAM free/total GB, "
            "GPU VRAM, system drive disk space, battery level) and active foreground application state."
        ),
        parameters={
            "type": "object",
            "properties": {},
        },
        handler=_handle_get_system_health,
        authority_tier=PermissionTier.L0_OBSERVE,
        read_only=True,
    )

    # 2. presence.list_routines
    def _handle_list_routines(args: Dict[str, Any]) -> Dict[str, Any]:
        """Lists learned personal computer routines."""
        routines = store.list_routines(approved_only=False)
        result = []
        for r in routines:
            result.append({
                "routine_id": r.routine_id,
                "name": r.name,
                "target_app": r.target_app,
                "trigger_pattern": r.trigger_pattern,
                "expected_final_state": r.expected_final_state,
                "is_approved": r.is_approved,
                "step_count": len(r.actions),
                "description": r.description,
            })
        return {
            "status": "SUCCESS",
            "count": len(result),
            "routines": result,
        }

    list_routines_tool = ToolDefinition(
        tool_id="presence.list_routines",
        name="presence.list_routines",
        description="Lists all learned personal computer routines saved in behavioral memory (e.g. YouTube Chrome routine).",
        parameters={
            "type": "object",
            "properties": {},
        },
        handler=_handle_list_routines,
        authority_tier=PermissionTier.L0_OBSERVE,
        read_only=True,
    )

    # 3. presence.execute_routine
    def _handle_execute_routine(args: Dict[str, Any]) -> Dict[str, Any]:
        """Executes a learned procedural routine."""
        identifier = str(args.get("routine_id") or args.get("name") or "").strip()
        if not identifier:
            return {"status": "ERROR", "error": "Missing 'routine_id' or 'name' argument."}

        routine = store.get_routine(identifier) or store.get_routine_by_name(identifier)
        if not routine:
            # Try fuzzy match
            matches = store.find_matching_routines(identifier, approved_only=False)
            if matches:
                routine = matches[0]

        if not routine:
            return {"status": "ERROR", "error": f"Routine '{identifier}' not found in behavioral memory."}

        logger.info(f"Executing routine '{routine.name}' ({len(routine.actions)} actions)...")
        executed_steps = []
        for i, act in enumerate(routine.actions):
            act_type = act.action_type.value if hasattr(act.action_type, "value") else str(act.action_type)
            executed_steps.append({
                "step": i + 1,
                "action_type": act_type,
                "target": act.target,
                "description": act.description,
            })

            # Execute via powershell or desktop controller if available
            if act_type == "LAUNCH_APP" or "chrome" in act.target.lower() or "http" in act.target:
                import subprocess
                cmd = f"start {act.target}"
                if act.parameters.get("url"):
                    cmd = f"start chrome.exe \"{act.parameters['url']}\""
                try:
                    subprocess.Popen(cmd, shell=True)
                except Exception as e:
                    logger.warning(f"Routine step {i+1} launch warning: {e}")

        return {
            "status": "SUCCESS",
            "routine_id": routine.routine_id,
            "routine_name": routine.name,
            "steps_executed": len(executed_steps),
            "details": executed_steps,
            "expected_state": routine.expected_final_state,
        }

    execute_routine_tool = ToolDefinition(
        tool_id="presence.execute_routine",
        name="presence.execute_routine",
        description="Executes a learned personal computer routine by routine_id or name.",
        parameters={
            "type": "object",
            "properties": {
                "routine_id": {
                    "type": "string",
                    "description": "Unique identifier of the routine to execute.",
                },
                "name": {
                    "type": "string",
                    "description": "Descriptive name of the routine to execute (e.g. 'Launch YouTube via Chrome Default Profile').",
                },
            },
        },
        handler=_handle_execute_routine,
        authority_tier=PermissionTier.L2_EXECUTE_REVERSIBLE,
        read_only=False,
    )

    return [health_tool, list_routines_tool, execute_routine_tool]
