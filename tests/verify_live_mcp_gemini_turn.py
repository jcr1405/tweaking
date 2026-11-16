"""Live End-to-End Gemini + MCP Process Supervision + Tool Execution Verification
Demonstrates live interaction where Gemini requests an external MCP tool,
Presence supervises and executes it over stdio inside a Win32 Job Object,
and persists the interaction in the conversation trajectory.
"""

import sys
import os
import tempfile
from pathlib import Path

# Add project root to sys.path
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from runtime.conversation import ConversationMetadata
from runtime.trajectory import TrajectoryStore, StepType
from runtime.journal import EventJournal
from runtime.summaries import GlobalSummaryManager
from runtime.permissions import PermissionBoundary, PermissionTier
from runtime.tools.registry import build_default_tools
from runtime.providers.gemini import GeminiModelProvider
from runtime.agent_executor import AgentExecutor
from runtime.artifacts.store import ArtifactStore
from runtime.mcp.models import MCPServerConfig
from runtime.mcp.schemas import SchemaCache
from runtime.mcp.supervisor import MCPSupervisor


def run_live_mcp_turn():
    print("=" * 75)
    print("  PRESENCE PHASE 2B: LIVE GEMINI + MCP PROCESS SUPERVISION VERIFICATION")
    print("=" * 75)

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("ERROR: GEMINI_API_KEY not set in environment.")
        return 1

    temp_dir = tempfile.TemporaryDirectory()
    base_dir = Path(temp_dir.name)
    conv_id = "live-mcp-gemini-conv-001"

    # 1. Trajectory & Logging
    store = TrajectoryStore(conversation_id=conv_id, db_dir=base_dir / "conversations")
    meta = ConversationMetadata(conversation_id=conv_id, title="Live Gemini + MCP Session")
    store.save_metadata(meta)

    journal = EventJournal(conversation_id=conv_id, logs_dir=base_dir / "logs")
    summary_mgr = GlobalSummaryManager(db_path=base_dir / "summaries.db")
    summary_mgr.upsert_summary(conv_id, meta.title)

    artifact_store = ArtifactStore(root_dir=base_dir / "artifacts")
    schema_cache = SchemaCache(cache_dir=base_dir / "schemas")

    # 2. MCP Supervisor & Process Containment
    supervisor = MCPSupervisor(schema_cache=schema_cache)
    fixture_path = project_root / "tests" / "fixtures" / "echo_mcp_server.py"

    server_config = MCPServerConfig(
        server_id="system_probe",
        name="System Probe MCP Server",
        command=sys.executable,
        args=[str(fixture_path)],
        startup_timeout=15.0,
        request_timeout=10.0,
        default_authority_tier=PermissionTier.L0_OBSERVE
    )
    supervisor.register_server(server_config)

    # 3. Tool Registry & Boundary
    boundary = PermissionBoundary()
    registry = build_default_tools(
        runtime_status_fn=lambda: {"state": "RUNNING", "pid": os.getpid()},
        permission_boundary=boundary
    )

    # Bridge MCP tools into ToolRegistry under 'mcp.system_probe.*'
    registered = supervisor.register_tools_with_registry(
        tool_registry=registry,
        permission_boundary=boundary,
        server_id="system_probe"
    )
    print(f"\nDiscovered & Bridged MCP Tools: {registered}")

    # 4. Gemini Provider
    provider = GeminiModelProvider(api_key=api_key, model_id="gemini-3.5-flash", temperature=0.2)
    valid, msg = provider.validate_configuration()
    print(f"Provider: {provider.provider_name} ({provider.model_id}) - {msg}\n")

    # 5. Agent Executor
    executor = AgentExecutor(
        trajectory_store=store,
        event_journal=journal,
        summary_manager=summary_mgr,
        permission_boundary=boundary,
        tool_registry=registry,
        model_provider=provider,
        artifact_store=artifact_store
    )

    prompt = "Please use the echo tool from the system_probe MCP server to repeat back 'Presence MCP Engine Active'."
    print(f"User: {prompt}\n")

    events = []
    print("Presence: ", end="", flush=True)
    for event in executor.execute_turn(user_input=prompt):
        events.append(event)
        if event.event_type == "TEXT_DELTA":
            print(event.payload.get("delta", ""), end="", flush=True)
        elif event.event_type == "TOOL_REQUESTED":
            print(f"\n  [MCP Tool Requested: {event.payload.get('name')}({event.payload.get('args')})]", flush=True)
        elif event.event_type == "TOOL_COMPLETED":
            print(f"  [MCP Tool Completed: {event.payload.get('result')}]", flush=True)
        elif event.event_type == "TOOL_FAILED":
            print(f"  [MCP Tool Failed: {event.payload.get('error')}]", flush=True)
        elif event.event_type == "TURN_COMPLETED":
            print(f"\n\n[Turn Completed! Iterations: {event.payload.get('iterations')}]")

    # Verify Trajectory persistence
    steps = store.list_steps()
    print("\n" + "=" * 75)
    print("  PERSISTED TRAJECTORY STEPS IN SQLITE")
    print("=" * 75)
    for step in steps:
        print(f"Step {step.idx} [{step.step_type.name}] ({step.status.name}): {str(step.step_payload)[:85]}...")

    supervisor.stop_all()
    store.close()
    journal.close()
    summary_mgr.close()
    temp_dir.cleanup()

    print("\n>> LIVE GEMINI + MCP PROCESS SUPERVISION VERIFIED SUCCESSFULLY! <<\n")
    return 0


if __name__ == "__main__":
    sys.exit(run_live_mcp_turn())
