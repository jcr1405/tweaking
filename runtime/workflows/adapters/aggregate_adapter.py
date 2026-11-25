"""Aggregate Execution Adapter for Workflow Nodes
Collects, synthesizes, and stores combined deliverables from upstream nodes.
"""

import json
import logging
from typing import Dict, Any, List

from runtime.workflows.adapters.base import BaseWorkflowAdapter, NodeExecutionContext, NodeExecutionResult
from runtime.workflows.models import WorkflowNode, NodeState

logger = logging.getLogger("presence.workflows.adapters.aggregate")


class AggregateWorkflowAdapter(BaseWorkflowAdapter):
    """Collects and merges results and artifacts from upstream dependency branches."""

    def execute(self, node: WorkflowNode, context: NodeExecutionContext) -> NodeExecutionResult:
        payload = node.payload or {}
        source_nodes = payload.get("source_nodes") or node.depends_on

        if not source_nodes:
            return NodeExecutionResult(
                state=NodeState.FAILED,
                error_message=f"AGGREGATE node '{node.node_id}' has no specified source_nodes or dependencies."
            )

        if context.cancellation_token and context.cancellation_token.is_cancelled:
            return NodeExecutionResult(
                state=NodeState.CANCELLED,
                error_message="Execution cancelled before aggregation."
            )

        output_format = payload.get("format", "markdown").lower()
        title = payload.get("title", f"Aggregated Report: {node.name}")

        sections: List[Dict[str, Any]] = []
        md_lines = [f"# {title}\n"]

        for src_id in source_nodes:
            ne = context.node_executions.get(src_id)
            if not ne:
                logger.warning(f"Aggregate node '{node.node_id}' source node '{src_id}' has no execution record.")
                continue

            sec_data = {
                "node_id": src_id,
                "state": ne.state.value,
                "duration_seconds": ne.duration_seconds,
                "artifact_id": ne.artifact_id,
                "payload": ne.result_payload,
                "error": ne.error_message,
            }

            content_text = ""
            # If node produced an artifact, retrieve text
            if ne.artifact_id and context.artifact_store:
                try:
                    read_res = context.artifact_store.read_artifact(
                        conversation_id=context.conversation_id,
                        artifact_id=ne.artifact_id,
                        mode="lines",
                        limit=5000
                    )
                    content_text = read_res.content
                except Exception as e:
                    logger.warning(f"Could not read artifact '{ne.artifact_id}' for aggregation: {e}")

            if not content_text and ne.result_payload:
                # If stdout or summary is in payload
                if isinstance(ne.result_payload, dict):
                    content_text = (
                        ne.result_payload.get("summary")
                        or ne.result_payload.get("stdout")
                        or json.dumps(ne.result_payload, indent=2)
                    )
                else:
                    content_text = str(ne.result_payload)

            sec_data["content"] = content_text
            sections.append(sec_data)

            md_lines.append(f"## Node: {src_id} (Status: {ne.state.value})\n")
            if ne.error_message:
                md_lines.append(f"> [!WARNING]\n> Error: {ne.error_message}\n")
            if content_text:
                md_lines.append(f"{content_text.strip()}\n")
            md_lines.append("---\n")

        full_markdown = "\n".join(md_lines)
        result_payload = {
            "title": title,
            "section_count": len(sections),
            "sources": source_nodes,
            "sections": sections,
            "summary_preview": full_markdown[:400]
        }

        # Store deliverable artifact
        artifact_id = None
        if context.artifact_store:
            try:
                meta = context.artifact_store.store_text_artifact(
                    conversation_id=context.conversation_id,
                    tool_call_id=node.node_id,
                    tool_name="presence.aggregate",
                    content_str=full_markdown
                )
                artifact_id = meta.artifact_id
                result_payload["artifact_id"] = artifact_id
            except Exception as e:
                logger.error(f"Failed to store aggregate artifact for node '{node.node_id}': {e}")

        return NodeExecutionResult(
            state=NodeState.COMPLETED,
            result_payload=result_payload,
            artifact_id=artifact_id
        )
