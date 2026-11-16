"""MCP Models and Data Structures
Defines data structures for Model Context Protocol (MCP) server configuration,
runtime process states, tool descriptors, and health telemetry.
"""

from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List

from runtime.permissions import PermissionTier


class MCPServerState(str, Enum):
    """Lifecycle states of a supervised MCP server subprocess."""
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    INITIALIZING = "INITIALIZING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    STOPPING = "STOPPING"
    FAILED = "FAILED"


@dataclass
class MCPServerConfig:
    """Configuration descriptor for a supervised MCP server."""
    server_id: str
    name: str
    command: str
    args: List[str] = field(default_factory=list)
    cwd: Optional[str] = None
    env: Dict[str, str] = field(default_factory=dict)
    startup_timeout: float = 10.0
    request_timeout: float = 30.0
    shutdown_timeout: float = 5.0
    max_restarts: int = 3
    restart_backoff_seconds: float = 1.0
    enabled: bool = True
    default_authority_tier: PermissionTier = PermissionTier.L0_OBSERVE
    tool_tiers: Dict[str, PermissionTier] = field(default_factory=dict)
    approved_tools: Optional[List[str]] = None
    auto_approve_tools: bool = False

    def is_tool_approved(self, tool_name: str) -> bool:
        """Determines if a tool is explicitly approved under local configuration policy (default-deny)."""
        if self.auto_approve_tools:
            return True
        if self.approved_tools is not None:
            return tool_name in self.approved_tools
        return False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "server_id": self.server_id,
            "name": self.name,
            "command": self.command,
            "args": self.args,
            "cwd": self.cwd,
            "env": {k: "***" if any(s in k.lower() for s in ["key", "secret", "token", "pwd"]) else v
                    for k, v in self.env.items()},
            "startup_timeout": self.startup_timeout,
            "request_timeout": self.request_timeout,
            "shutdown_timeout": self.shutdown_timeout,
            "max_restarts": self.max_restarts,
            "enabled": self.enabled,
            "default_authority_tier": self.default_authority_tier.name,
            "tool_tiers": {k: v.name for k, v in self.tool_tiers.items()},
            "approved_tools": self.approved_tools,
            "auto_approve_tools": self.auto_approve_tools,
        }


@dataclass
class MCPToolInfo:
    """Discovered MCP tool metadata and parameter schema."""
    server_id: str
    tool_name: str
    description: str
    input_schema: Dict[str, Any]
    approved: bool = False

    @property
    def namespaced_name(self) -> str:
        """Namespaced tool identifier (e.g. mcp.fs_reader.read_file)."""
        return f"mcp.{self.server_id}.{self.tool_name}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "server_id": self.server_id,
            "tool_name": self.tool_name,
            "namespaced_name": self.namespaced_name,
            "description": self.description,
            "input_schema": self.input_schema,
            "approved": self.approved
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MCPToolInfo":
        return cls(
            server_id=data["server_id"],
            tool_name=data["tool_name"],
            description=data.get("description", ""),
            input_schema=data.get("input_schema", {}),
            approved=data.get("approved", False)
        )


@dataclass
class MCPServerStatus:
    """Runtime health and diagnostic metrics for a supervised MCP server."""
    server_id: str
    state: MCPServerState
    pid: Optional[int] = None
    restarts: int = 0
    tools_count: int = 0
    memory_mb: float = 0.0
    last_error: Optional[str] = None
    protocol_version: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "server_id": self.server_id,
            "state": self.state.value,
            "pid": self.pid,
            "restarts": self.restarts,
            "tools_count": self.tools_count,
            "memory_mb": round(self.memory_mb, 2),
            "last_error": self.last_error,
            "protocol_version": self.protocol_version
        }
