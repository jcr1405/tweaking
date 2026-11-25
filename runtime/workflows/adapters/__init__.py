"""Workflow Adapters Package
Exports execution adapters and resolution factory.
"""

from runtime.workflows.models import ExecutionType
from runtime.workflows.adapters.base import BaseWorkflowAdapter, NodeExecutionContext, NodeExecutionResult
from runtime.workflows.adapters.tool_adapter import ToolWorkflowAdapter
from runtime.workflows.adapters.process_adapter import ProcessWorkflowAdapter
from runtime.workflows.adapters.subagent_adapter import SubagentWorkflowAdapter
from runtime.workflows.adapters.aggregate_adapter import AggregateWorkflowAdapter
from runtime.workflows.adapters.verify_adapter import VerifyWorkflowAdapter
from runtime.workflows.adapters.local_operation_adapter import LocalOperationWorkflowAdapter
from runtime.workflows.adapters.desktop_adapter import DesktopWorkflowAdapter

__all__ = [
    "BaseWorkflowAdapter",
    "NodeExecutionContext",
    "NodeExecutionResult",
    "ToolWorkflowAdapter",
    "ProcessWorkflowAdapter",
    "SubagentWorkflowAdapter",
    "AggregateWorkflowAdapter",
    "VerifyWorkflowAdapter",
    "LocalOperationWorkflowAdapter",
    "DesktopWorkflowAdapter",
    "get_adapter_for_type",
]


def get_adapter_for_type(execution_type: ExecutionType) -> BaseWorkflowAdapter:
    """Returns the appropriate execution adapter instance for an ExecutionType."""
    if execution_type == ExecutionType.TOOL:
        return ToolWorkflowAdapter()
    elif execution_type == ExecutionType.PROCESS:
        return ProcessWorkflowAdapter()
    elif execution_type == ExecutionType.SUBAGENT:
        return SubagentWorkflowAdapter()
    elif execution_type == ExecutionType.AGGREGATE:
        return AggregateWorkflowAdapter()
    elif execution_type == ExecutionType.VERIFY:
        return VerifyWorkflowAdapter()
    elif execution_type == ExecutionType.LOCAL_OPERATION:
        return LocalOperationWorkflowAdapter()
    elif execution_type == ExecutionType.DESKTOP_OPERATION:
        return DesktopWorkflowAdapter()
    else:
        raise ValueError(f"Unsupported ExecutionType: {execution_type}")
