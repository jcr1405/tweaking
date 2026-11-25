"""Base Execution Adapter Interface for Workflow Nodes
Defines execution context, variable resolution, and adapter contract.
Enforces strict dependency authorization, type preservation, and path security.
"""

import re
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

from runtime.workflows.models import WorkflowNode, WorkflowNodeExecution, NodeState
from runtime.agent_executor import CancellationToken
from runtime.artifacts.store import ArtifactStore


class SecurityValidationError(Exception):
    """Raised when an unauthorized predecessor reference or path traversal is detected."""
    pass


@dataclass
class NodeExecutionContext:
    """Runtime context and subsystem handles provided to a node execution adapter."""
    workflow_run_id: str
    conversation_id: str
    node_executions: Dict[str, WorkflowNodeExecution] = field(default_factory=dict)
    cancellation_token: Optional[CancellationToken] = None
    artifact_store: Optional[ArtifactStore] = None
    tool_registry: Optional[Any] = None
    process_manager: Optional[Any] = None
    subagent_manager: Optional[Any] = None
    permission_boundary: Optional[Any] = None
    local_supervisor: Optional[Any] = None
    desktop_controller: Optional[Any] = None

    def get_predecessor_results(self, depends_on: Optional[List[str]] = None) -> Dict[str, Any]:
        """Collects clean output payloads and artifacts from authorized upstream dependencies.
        Rejects cross-workflow references and uncompleted predecessors.
        """
        results: Dict[str, Any] = {}
        target_nids = depends_on if depends_on is not None else list(self.node_executions.keys())
        for nid in target_nids:
            if nid in self.node_executions:
                ne = self.node_executions[nid]
                # Enforce workflow isolation: must belong to the exact same workflow run
                if getattr(ne, "workflow_run_id", self.workflow_run_id) != self.workflow_run_id:
                    continue
                results[nid] = {
                    "state": ne.state.value if hasattr(ne.state, "value") else str(ne.state),
                    "result_payload": ne.result_payload or {},
                    "artifact_id": ne.artifact_id,
                    "error_message": ne.error_message,
                }
        return results

    def _extract_nested_field(self, obj: Any, path_parts: List[str]) -> Tuple[bool, Any]:
        """Recursively traverses nested dictionaries or object attributes."""
        current = obj
        for part in path_parts:
            if current is None:
                return False, None
            if isinstance(current, dict):
                if part in current:
                    current = current[part]
                else:
                    return False, None
            elif hasattr(current, part):
                current = getattr(current, part)
            else:
                return False, None
        return True, current

    def _resolve_key(
        self,
        ref: str,
        authorized_dependencies: Optional[List[str]] = None
    ) -> Tuple[bool, Any]:
        """Resolves a single reference string (e.g. 'step1.output.key' or 'step1.artifact_id').
        Enforces:
        1. Target node must exist.
        2. Target node must belong to this workflow run.
        3. Target node must be in authorized_dependencies (if specified).
        4. Target node must be COMPLETED.
        """
        parts = ref.strip().split(".")
        if len(parts) < 2:
            return False, None

        nid = parts[0]
        field_path = parts[1:]

        # 1. Dependency authorization
        if authorized_dependencies is not None and nid not in authorized_dependencies:
            return False, None

        ne = self.node_executions.get(nid)
        if not ne:
            return False, None

        # 2. Workflow isolation
        if getattr(ne, "workflow_run_id", self.workflow_run_id) != self.workflow_run_id:
            return False, None

        # 3. Completion check: only COMPLETED nodes yield valid results
        if ne.state != NodeState.COMPLETED:
            return False, None

        # 4. Resolve artifact_id
        if field_path == ["artifact_id"]:
            if ne.artifact_id:
                return True, ne.artifact_id
            return False, None

        # 5. Resolve field from result_payload
        if ne.result_payload and isinstance(ne.result_payload, dict):
            # Check direct or stripped 'output' prefix
            found, val = self._extract_nested_field(ne.result_payload, field_path)
            if found:
                return True, val
            if field_path[0] in ("output", "result", "result_payload") and len(field_path) > 1:
                found, val = self._extract_nested_field(ne.result_payload, field_path[1:])
                if found:
                    return True, val

        return False, None

    def resolve_template_variables(
        self,
        text: str,
        authorized_dependencies: Optional[List[str]] = None
    ) -> str:
        """Resolves template placeholders such as {{ node_id.field }} or {node_id.field}
        strictly from authorized, completed predecessor results.
        """
        if not isinstance(text, str):
            return text

        # Regex for {{ node.field }} or {node.field}
        pattern = re.compile(r"\{\{?\s*([a-zA-Z0-9_\-\.]+)\s*\}?\}")

        def _replace_match(match: re.Match) -> str:
            ref = match.group(1)
            found, val = self._resolve_key(ref, authorized_dependencies)
            if found:
                return str(val)
            # Leave untouched if not authorized or not found
            return match.group(0)

        return pattern.sub(_replace_match, text)

    def resolve_value(
        self,
        value: Any,
        authorized_dependencies: Optional[List[str]] = None
    ) -> Any:
        """Recursively resolves values while preserving structured data types.
        If a string exactly matches a placeholder `{{ nid.field }}` or `{nid.field}`,
        the original typed object (dict, list, int, bool, etc.) is returned directly.
        """
        if isinstance(value, str):
            single_match = re.fullmatch(r"\{\{?\s*([a-zA-Z0-9_\-\.]+)\s*\}?\}", value.strip())
            if single_match:
                ref = single_match.group(1)
                found, val = self._resolve_key(ref, authorized_dependencies)
                if found:
                    return val
                return value
            return self.resolve_template_variables(value, authorized_dependencies)

        elif isinstance(value, dict):
            return {k: self.resolve_value(v, authorized_dependencies) for k, v in value.items()}

        elif isinstance(value, list):
            return [self.resolve_value(item, authorized_dependencies) for item in value]

        return value

    def validate_path_argument(
        self,
        path_str: str,
        allowed_root: Optional[str] = None
    ) -> str:
        """Validates a path string to prevent path traversal, UNC paths, and device names.
        
        Raises:
            SecurityValidationError: If path escapes allowed root or contains malicious markers.
        """
        if not path_str or not isinstance(path_str, str):
            raise SecurityValidationError("Path argument must be a non-empty string.")

        # Rejection of UNC paths (\\server\share)
        if path_str.startswith("\\\\") or path_str.startswith("//"):
            raise SecurityValidationError(f"UNC network paths are prohibited: {path_str}")

        # Rejection of Windows reserved device names
        base_name = Path(path_str).stem.upper()
        reserved_devices = {
            "CON", "PRN", "AUX", "NUL",
            "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
            "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
        }
        if base_name in reserved_devices:
            raise SecurityValidationError(f"Reserved Windows device name prohibited: {base_name}")

        p = Path(path_str)
        if allowed_root:
            root_path = Path(allowed_root).resolve()
            try:
                resolved_p = (root_path / p).resolve() if not p.is_absolute() else p.resolve()
                resolved_p.relative_to(root_path)
            except ValueError:
                raise SecurityValidationError(
                    f"Path '{path_str}' attempts directory traversal outside allowed root '{allowed_root}'."
                )
            return str(resolved_p)

        # Basic path traversal check if no allowed_root specified
        if ".." in p.parts:
            raise SecurityValidationError(f"Directory traversal ('..') detected in path: {path_str}")

        return str(p)


@dataclass
class NodeExecutionResult:
    """Normalized result returned by an execution adapter."""
    state: NodeState
    result_payload: Optional[Dict[str, Any]] = None
    artifact_id: Optional[str] = None
    error_message: Optional[str] = None


class BaseWorkflowAdapter(ABC):
    """Abstract base class for all node execution adapters."""

    @abstractmethod
    def execute(self, node: WorkflowNode, context: NodeExecutionContext) -> NodeExecutionResult:
        """Executes the node workload deterministically.
        
        Args:
            node: The WorkflowNode definition.
            context: The runtime execution context and subsystem references.
            
        Returns:
            NodeExecutionResult containing final state, output payload, and artifact IDs.
        """
        pass
