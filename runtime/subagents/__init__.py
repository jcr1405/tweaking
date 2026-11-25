"""Presence Subagent Subsystem
Provides isolated child agent sessions, concurrency and recursion bounds,
role-based tool filtering, and receipt integration.
"""

from runtime.subagents.models import (
    SubagentRole,
    SubagentStatus,
    SubagentConfig,
    SubagentExecutionResult,
    ManagedSubagent,
)
from runtime.subagents.manager import SubagentManager

__all__ = [
    "SubagentRole",
    "SubagentStatus",
    "SubagentConfig",
    "SubagentExecutionResult",
    "ManagedSubagent",
    "SubagentManager",
]
