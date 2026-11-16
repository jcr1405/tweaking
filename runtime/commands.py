"""Command Dispatcher Subsystem
Handles deterministic local commands with strict permission mediation.
Bypasses LLMs for fast, predictable system queries and actions.
"""

import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Any
from .permissions import PermissionBoundary, AuthorityLevel, PermissionDeniedError

logger = logging.getLogger("presence.commands")


@dataclass(frozen=True)
class CommandResult:
    success: bool
    output: str
    data: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


CommandHandler = Callable[[List[str], Any], CommandResult]


class CommandDispatcher:
    """Deterministic local command dispatcher."""

    def __init__(self, permissions: PermissionBoundary, runtime_ref: Any = None):
        self.permissions = permissions
        self.runtime_ref = runtime_ref
        self._handlers: Dict[str, CommandHandler] = {}
        self._action_mappings: Dict[str, str] = {}
        self._register_default_commands()

    def register(self, command_name: str, action_name: str, handler: CommandHandler) -> None:
        """Registers a command name mapped to an authority policy action."""
        cmd_key = command_name.strip().lower()
        self._handlers[cmd_key] = handler
        self._action_mappings[cmd_key] = action_name

    def _register_default_commands(self) -> None:
        """Registers core deterministic commands for Milestone 0."""
        self.register("status", "system.status", self._handle_status)
        self.register("help", "help.read", self._handle_help)
        self.register("config", "config.read", self._handle_config)
        self.register("shutdown", "runtime.shutdown", self._handle_shutdown)

    def execute(self, command_line: str, granted_consent: bool = False) -> CommandResult:
        """Parses and executes a command string under strict permission gate."""
        tokens = command_line.strip().split()
        if not tokens:
            return CommandResult(success=True, output="")

        cmd_name = tokens[0].lower()
        args = tokens[1:]

        handler = self._handlers.get(cmd_name)
        if not handler:
            return CommandResult(
                success=False,
                output=f"Unknown command: '{cmd_name}'. Type 'help' for available commands.",
                error="COMMAND_NOT_FOUND",
            )

        action_name = self._action_mappings.get(cmd_name, f"cmd.{cmd_name}")

        # Check permissions before invoking handler
        try:
            self.permissions.authorize(action_name, context={"args": args}, granted_consent=granted_consent)
        except PermissionDeniedError as pde:
            return CommandResult(
                success=False,
                output=f"Permission Denied: {pde}",
                error="PERMISSION_DENIED",
            )

        try:
            return handler(args, self.runtime_ref)
        except Exception as e:
            logger.error(f"Error executing command '{cmd_name}': {e}", exc_info=True)
            return CommandResult(
                success=False,
                output=f"Execution error: {e}",
                error=str(e),
            )

    # Core Command Handlers
    def _handle_status(self, args: List[str], runtime: Any) -> CommandResult:
        if not runtime:
            return CommandResult(success=True, output="Runtime active (no runtime reference attached).")

        health = runtime.get_health_status()
        mem = health.get("memory", {})
        output_lines = [
            f"=== Presence Runtime Status ===",
            f"State:        {health.get('state')}",
            f"Instance ID:  {health.get('instance_id')}",
            f"PID:          {health.get('pid')}",
            f"Uptime:       {health.get('uptime_seconds', 0):.2f}s",
            f"Working Set:  {mem.get('working_set_mb', 0):.2f} MB",
            f"Private RAM:  {mem.get('private_mb', 0):.2f} MB",
            f"Events Fired: {health.get('events_published', 0)}",
            f"Clean Recover:{health.get('clean_recovery', True)}",
        ]
        return CommandResult(success=True, output="\n".join(output_lines), data=health)

    def _handle_help(self, args: List[str], runtime: Any) -> CommandResult:
        commands = [
            ("status", "Display runtime state, uptime, memory, and subsystem health"),
            ("config [key] [val]", "Inspect or mutate persistent configuration parameters"),
            ("help", "Show this command reference"),
            ("shutdown", "Gracefully terminate the presence runtime"),
        ]
        output_lines = ["Available Commands:"]
        for cmd, desc in sorted(commands):
            output_lines.append(f"  {cmd:<20} - {desc}")
        return CommandResult(success=True, output="\n".join(output_lines), data={"commands": dict(commands)})

    def _handle_config(self, args: List[str], runtime: Any) -> CommandResult:
        if not runtime or not runtime.persistence:
            return CommandResult(success=False, output="Persistence engine unavailable.", error="NO_PERSISTENCE")

        # 1. No args -> dump all
        if not args:
            all_cfg = runtime.persistence.get_all_config()
            if not all_cfg:
                return CommandResult(success=True, output="No persistent configuration set.")
            lines = ["Current Configuration:"]
            for k, v in sorted(all_cfg.items()):
                lines.append(f"  {k} = {v}")
            return CommandResult(success=True, output="\n".join(lines), data=all_cfg)

        # 2. One arg -> get key
        if len(args) == 1:
            key = args[0]
            val = runtime.persistence.get_config(key)
            if val is None:
                return CommandResult(success=False, output=f"Config key '{key}' not found.", error="KEY_NOT_FOUND")
            return CommandResult(success=True, output=f"{key} = {val}", data={"key": key, "value": val})

        # 3. Two+ args -> set key (requires L2 write authorization)
        key = args[0]
        val = " ".join(args[1:])
        try:
            self.permissions.authorize("config.write", context={"key": key, "value": val})
            runtime.persistence.set_config(key, val)
            return CommandResult(success=True, output=f"Updated config: {key} = {val}", data={"key": key, "value": val})
        except PermissionDeniedError as pde:
            return CommandResult(success=False, output=f"Permission Denied: {pde}", error="PERMISSION_DENIED")

    def _handle_shutdown(self, args: List[str], runtime: Any) -> CommandResult:
        if not runtime:
            return CommandResult(success=False, output="Cannot shutdown: No runtime reference attached.")
        runtime.stop()
        return CommandResult(success=True, output="Runtime shutdown initiated.")
