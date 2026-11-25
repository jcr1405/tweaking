"""Task Ledger Markdown Projection Renderer
Deterministically projects the authoritative SQLite task ledger state
into a clean, human-readable Markdown artifact (TASK_LEDGER.md).
"""

import time
from typing import Dict, Any, List, Optional
from runtime.task_planning.models import TaskPlan, PlanStatus
from runtime.workflows.models import WorkflowRun, NodeState


class LedgerProjectionRenderer:
    """Renders human-readable markdown summaries and checklists of TaskPlans."""

    STATE_ICONS = {
        "COMPLETED": "[x]",
        "RUNNING": "[/]",
        "IN_PROGRESS": "[/]",
        "READY": "[ ]",
        "PENDING": "[ ]",
        "WAITING_DEPENDENCIES": "[ ]",
        "FAILED": "[!]",
        "CANCELLED": "[-]",
        "SKIPPED": "[-]",
        "INTERRUPTED": "[?]",
        "AWAITING_APPROVAL": "[?]",
        "AWAITING_CLARIFICATION": "[?]",
        "OUTCOME_UNKNOWN": "[?]",
        "PLAN_UNAVAILABLE": "[!]",
        "VALIDATION_FAILED": "[!]",
    }

    @classmethod
    def render_markdown(
        cls,
        plan: TaskPlan,
        node_states: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> str:
        """Constructs a structured Markdown task ledger document."""
        node_states = node_states or {}

        # Calculate progress statistics
        total_nodes = len(plan.nodes)
        completed_count = 0
        failed_count = 0
        in_progress_count = 0

        for node in plan.nodes:
            st = node_states.get(node.node_id, {}).get("state", "PENDING")
            if st == "COMPLETED":
                completed_count += 1
            elif st == "FAILED":
                failed_count += 1
            elif st in ("RUNNING", "IN_PROGRESS"):
                in_progress_count += 1

        percent = int((completed_count / max(1, total_nodes)) * 100)

        lines: List[str] = []
        lines.append(f"# Task Ledger: {plan.title}\n")
        lines.append(
            f"**Plan ID:** `{plan.plan_id}` | "
            f"**Version:** {plan.plan_version} | "
            f"**Status:** `{plan.status.value}` | "
            f"**Progress:** {completed_count}/{total_nodes} ({percent}%)\n"
        )
        lines.append(f"> **Goal:** {plan.user_goal}\n")

        if plan.description:
            lines.append(f"{plan.description}\n")

        lines.append("## Execution Plan & Live Status\n")

        # Group nodes by phase
        phases: Dict[int, List[Any]] = {}
        for n in plan.nodes:
            phases.setdefault(n.phase, []).append(n)

        for p_num in sorted(phases.keys()):
            lines.append(f"### Phase {p_num}\n")
            for node in phases[p_num]:
                st_info = node_states.get(node.node_id, {})
                st_val = st_info.get("state", "PENDING")
                icon = cls.STATE_ICONS.get(st_val, "[ ]")
                duration = st_info.get("duration_seconds")
                duration_str = f" ({duration:.2f}s)" if duration is not None else ""

                lines.append(f"- {icon} **{node.title}**{duration_str}")
                lines.append(f"  - *Type:* `{node.execution_type.value}` | *ID:* `{node.node_id}` | *Status:* `{st_val}`")
                if node.depends_on:
                    lines.append(f"  - *Depends on:* {', '.join(f'`{d}`' for d in node.depends_on)}")

                artifact_id = st_info.get("artifact_id")
                if artifact_id:
                    lines.append(f"  - *Artifact Evidence:* `{artifact_id}`")

                err = st_info.get("error") or st_info.get("error_message")
                if err:
                    lines.append(f"  - > [!WARNING]\n  - > Error: {err}")

            lines.append("")

        if plan.expected_deliverables:
            lines.append("## Expected Deliverables")
            for deliv in plan.expected_deliverables:
                lines.append(f"- {deliv}")
            lines.append("")

        lines.append(f"*Last Updated: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}*")
        return "\n".join(lines)
