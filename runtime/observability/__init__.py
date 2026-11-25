"""Presence Runtime Observability Subsystem
Provides non-intrusive, read-only diagnostic snapshots, hierarchical task tracking,
execution traces, and thread-safe event streaming for terminal and desktop interfaces.
"""

from runtime.observability.models import (
    TaskState,
    TaskType,
    TaskNode,
    NormalizedEvent,
    ConversationSummaryDTO,
    TurnDetailDTO,
    TraceEventDTO,
    ContextInspectionDTO,
    MCPServerViewDTO,
    MCPToolViewDTO,
    RuntimeSnapshot
)
from runtime.observability.task_registry import ActiveTaskRegistry
from runtime.observability.event_bridge import ObservabilityEventBridge
from runtime.observability.redaction import sanitize_terminal_output, redact_sensitive_string, sanitize_and_redact, redact_data_structure
from runtime.observability.service import RuntimeObservabilityService

__all__ = [
    "TaskState",
    "TaskType",
    "TaskNode",
    "NormalizedEvent",
    "ConversationSummaryDTO",
    "TurnDetailDTO",
    "TraceEventDTO",
    "ContextInspectionDTO",
    "MCPServerViewDTO",
    "MCPToolViewDTO",
    "RuntimeSnapshot",
    "ActiveTaskRegistry",
    "ObservabilityEventBridge",
    "sanitize_terminal_output",
    "redact_sensitive_string",
    "sanitize_and_redact",
    "redact_data_structure",
    "RuntimeObservabilityService",
]
