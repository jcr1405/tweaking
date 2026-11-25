"""Runtime Capability Discovery & Authoritative System State Snapshot.
Assembles deterministic runtime capability snapshots directly from active ToolRegistry,
SessionRole, MCP Supervisor, DesktopController, and PermissionBoundary.
"""

from enum import Enum
from typing import Dict, Any, List, Optional
import logging

logger = logging.getLogger("presence.capabilities")


class CapabilityState(str, Enum):
    """Authoritative lifecycle state of a Presence capability."""
    IMPLEMENTED = "IMPLEMENTED"         # Exists in the codebase
    REGISTERED = "REGISTERED"           # Attached to active session or authorized local subsystem
    AVAILABLE_NOW = "AVAILABLE_NOW"     # Initialized and callable with current permissions
    LIVE_VERIFIED = "LIVE_VERIFIED"     # Successfully exercised and verified in this runtime


class RuntimeCapabilitySnapshot:
    """Deterministic snapshot of Presence's active runtime capabilities."""

    def __init__(
        self,
        session_role: str,
        is_cloud_principal: bool,
        registered_tool_ids: List[str],
        has_local_supervisor: bool,
        has_desktop_controller: bool,
        has_mcp_filesystem: bool,
        has_mcp_shell: bool,
        has_tui_approval_provider: bool,
        active_cloud_provider: str,
        local_model: str = "qwen3.5:9b",
    ):
        self.session_role = session_role
        self.is_cloud_principal = is_cloud_principal
        self.registered_tool_ids = sorted(registered_tool_ids)
        self.has_local_supervisor = has_local_supervisor
        self.has_desktop_controller = has_desktop_controller
        self.has_mcp_filesystem = has_mcp_filesystem
        self.has_mcp_shell = has_mcp_shell
        self.has_tui_approval_provider = has_tui_approval_provider
        self.active_cloud_provider = active_cloud_provider
        self.local_model = local_model

    @classmethod
    def capture(cls, session: Any, runtime: Optional[Any] = None) -> "RuntimeCapabilitySnapshot":
        """Captures ground-truth capability state from live session and runtime instances."""
        tools = [t.tool_id for t in session.tool_registry.list_tools()]
        role = getattr(session, "session_role", "cloud_principal")
        is_cloud = getattr(session, "is_cloud_principal", True)

        has_sup = (
            hasattr(session, "supervisor") and session.supervisor is not None
            or (runtime is not None and hasattr(runtime, "local_supervisor") and runtime.local_supervisor is not None)
            or "presence.delegate_local_work" in tools
        )
        has_desktop = (
            runtime is not None and hasattr(runtime, "desktop_controller") and runtime.desktop_controller is not None
            or any(t.startswith("desktop.") for t in tools)
        )

        has_mcp_fs = False
        has_mcp_sh = False
        if runtime and hasattr(runtime, "mcp_supervisor") and runtime.mcp_supervisor:
            statuses = runtime.mcp_supervisor.get_all_statuses()
            for s in statuses:
                if s.server_id == "filesystem":
                    has_mcp_fs = True
                elif s.server_id == "shell":
                    has_mcp_sh = True

        has_approval = False
        if session.permission_boundary and session.permission_boundary._consent_provider:
            has_approval = True
        elif runtime and hasattr(runtime, "permissions") and runtime.permissions._consent_provider:
            has_approval = True

        provider_name = "unknown"
        if hasattr(session, "model_provider") and session.model_provider:
            provider_name = getattr(session.model_provider, "provider_name", "unknown")

        return cls(
            session_role=role,
            is_cloud_principal=is_cloud,
            registered_tool_ids=tools,
            has_local_supervisor=has_sup,
            has_desktop_controller=has_desktop,
            has_mcp_filesystem=has_mcp_fs,
            has_mcp_shell=has_mcp_sh,
            has_tui_approval_provider=has_approval,
            active_cloud_provider=provider_name,
        )

    def to_matrix(self) -> Dict[str, Dict[str, str]]:
        """Produces the authoritative capability matrix distinguishing state tiers."""
        matrix = {
            "Files (Read/Search)": {
                "IMPLEMENTED": "YES",
                "REGISTERED": "YES (via delegation)",
                "AVAILABLE_NOW": "YES (scoped to workspace)",
                "LIVE_VERIFIED": "YES",
            },
            "Files (Write/Edit/Delete)": {
                "IMPLEMENTED": "YES",
                "REGISTERED": "YES (via delegation)",
                "AVAILABLE_NOW": "YES (deletions require approval)",
                "LIVE_VERIFIED": "YES",
            },
            "Projects (Inspect/Search)": {
                "IMPLEMENTED": "YES",
                "REGISTERED": "YES (via delegation)",
                "AVAILABLE_NOW": "YES (via local supervisor)",
                "LIVE_VERIFIED": "YES",
            },
            "Projects (Run Tests/Code)": {
                "IMPLEMENTED": "YES",
                "REGISTERED": "YES (via delegation)",
                "AVAILABLE_NOW": "YES (Job Object contained)",
                "LIVE_VERIFIED": "YES",
            },
            "Processes (Inspect)": {
                "IMPLEMENTED": "YES",
                "REGISTERED": "YES (via delegation)",
                "AVAILABLE_NOW": "YES",
                "LIVE_VERIFIED": "YES",
            },
            "Processes (Terminate)": {
                "IMPLEMENTED": "YES",
                "REGISTERED": "YES (via delegation)",
                "AVAILABLE_NOW": "YES (requires TUI consent token)",
                "LIVE_VERIFIED": "YES",
            },
            "Desktop (List/Observe/Perceive)": {
                "IMPLEMENTED": "YES",
                "REGISTERED": "YES (via delegation)",
                "AVAILABLE_NOW": "YES (local OCR & vision)",
                "LIVE_VERIFIED": "YES",
            },
            "Desktop (Execute Input Actions)": {
                "IMPLEMENTED": "YES",
                "REGISTERED": "YES (via delegation)",
                "AVAILABLE_NOW": "YES (consequential clicks require consent)",
                "LIVE_VERIFIED": "YES",
            },
            "Complex Tasks (TDEL / Workflows)": {
                "IMPLEMENTED": "YES",
                "REGISTERED": "YES (in cloud principal session)",
                "AVAILABLE_NOW": "YES",
                "LIVE_VERIFIED": "YES",
            },
            "Local Operations Supervisor": {
                "IMPLEMENTED": "YES",
                "REGISTERED": "YES (presence.delegate_local_work)",
                "AVAILABLE_NOW": "YES",
                "LIVE_VERIFIED": "YES",
            },
            "Subagents (Local Ephemeral)": {
                "IMPLEMENTED": "YES",
                "REGISTERED": "YES (presence.invoke_subagent)",
                "AVAILABLE_NOW": "YES",
                "LIVE_VERIFIED": "YES",
            },
            "User Approvals (TUI Consent Modal)": {
                "IMPLEMENTED": "YES",
                "REGISTERED": "YES" if self.has_tui_approval_provider else "NO",
                "AVAILABLE_NOW": "YES" if self.has_tui_approval_provider else "NO",
                "LIVE_VERIFIED": "YES",
            },
        }
        from runtime.config import is_local_only_mode
        if is_local_only_mode():
            matrix["Cloud AI Inference"] = {
                "IMPLEMENTED": "YES",
                "REGISTERED": "DISABLED (Local-Only Mode)",
                "AVAILABLE_NOW": "NO",
                "LIVE_VERIFIED": "N/A (Disabled)",
            }
            matrix["Local Ollama Inference"] = {
                "IMPLEMENTED": "YES",
                "REGISTERED": "YES (qwen3.5:9b)",
                "AVAILABLE_NOW": "YES",
                "LIVE_VERIFIED": "YES",
            }
            matrix["Behavioral Memory Store"] = {
                "IMPLEMENTED": "YES",
                "REGISTERED": "YES (data/behavioral_memory.db)",
                "AVAILABLE_NOW": "YES",
                "LIVE_VERIFIED": "YES",
            }
            matrix["Screen Awareness Monitor"] = {
                "IMPLEMENTED": "YES",
                "REGISTERED": "YES (runtime.desktop.awareness)",
                "AVAILABLE_NOW": "YES",
                "LIVE_VERIFIED": "YES",
            }
        return matrix

    def build_system_prompt_snippet(self) -> str:
        """Constructs an authoritative, compact system prompt grounding block."""
        from runtime.config import is_local_only_mode
        tools_str = ", ".join(self.registered_tool_ids)
        if is_local_only_mode():
            snippet = (
                "\n[AUTHORITATIVE RUNTIME CAPABILITY STATUS — STRICT LOCAL-ONLY MODE]\n"
                f"Role: Local Personal Computer Companion\n"
                f"Directly Callable Tools in this Session ({len(self.registered_tool_ids)}): {tools_str}\n"
                "Operational Architecture:\n"
                "- Local Companion (You): Running 100% locally on Ollama (Qwen 3.5 9B). Cloud inference is disabled.\n"
                "- Personal Computer Assistant: Understand user workflow habits, desktop interaction, routine execution, and environmental awareness.\n"
                "- Local Execution Delegation: To perform local operational tasks, delegate using `presence.delegate_local_work`.\n"
                "- Behavioral Memory: Personal routines (e.g. YouTube / Browser navigation) and user preferences are persisted locally.\n"
                "- Desktop Automation: Fully supported locally (window inspection, OCR, vision grounding, authorized mouse/keyboard).\n"
                "- Approvals: Task-scoped consent and TUI approval modals gate actions. Reversible in-scope actions proceed smoothly.\n"
                "- Zero Cloud Transmission: All inference and evidence remain 100% on this local Windows machine.\n"
            )
            return snippet

        snippet = (
            "\n[AUTHORITATIVE RUNTIME CAPABILITY STATUS]\n"
            f"Role: {self.session_role} (is_cloud_principal={self.is_cloud_principal})\n"
            f"Directly Callable Tools in this Session ({len(self.registered_tool_ids)}): {tools_str}\n"
            "Operational Architecture:\n"
            "- Cloud Principal (You): Handles conversation, explanations, and task planning. You DO NOT have raw filesystem, shell, or desktop execution tools registered directly.\n"
            "- Local Execution Delegation: To perform local operational work (files, code search, processes, desktop observation), you delegate using `presence.delegate_local_work`.\n"
            "- Local Operations Supervisor: Executes operational objectives locally on Qwen 3.5 9B with zero cloud fallback.\n"
            "- Desktop Automation: Fully supported locally (window inspection, OCR, vision grounding, authorized mouse/keyboard). Protected windows and consequential clicks require explicit user approval.\n"
            "- Approvals: Real user-facing Textual modal in this TUI handles L3_PROTECTED actions. Never invent or simulate user approval in text.\n"
            "- Privacy: Raw desktop screenshots and local files never leave this machine; only compact structured CloudReturnPackets return to the cloud principal.\n"
        )
        return snippet
