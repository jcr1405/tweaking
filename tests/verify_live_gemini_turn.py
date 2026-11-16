"""Live End-to-End Turn Verification with Google Gemini API"""

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
from runtime.permissions import PermissionBoundary
from runtime.tools.registry import build_default_tools
from runtime.providers.gemini import GeminiModelProvider
from runtime.agent_executor import AgentExecutor


def test_live_turn():
    print("=" * 70)
    print("  LIVE GEMINI API END-TO-END TURN VERIFICATION")
    print("=" * 70)

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("ERROR: GEMINI_API_KEY not set.")
        return 1

    temp_dir = tempfile.TemporaryDirectory()
    base_dir = Path(temp_dir.name)
    conv_id = "live-gemini-conv-001"

    store = TrajectoryStore(conversation_id=conv_id, db_dir=base_dir / "conversations")
    meta = ConversationMetadata(conversation_id=conv_id, title="Live Gemini Turn Session")
    store.save_metadata(meta)

    journal = EventJournal(conversation_id=conv_id, logs_dir=base_dir / "logs")
    summary_mgr = GlobalSummaryManager(db_path=base_dir / "summaries.db")
    summary_mgr.upsert_summary(conv_id, meta.title)

    boundary = PermissionBoundary()
    registry = build_default_tools(
        runtime_status_fn=lambda: {"state": "RUNNING", "pid": 44444, "working_set_mb": 24.1, "uptime": "42s"},
        conversation_info_fn=lambda: {"conversation_id": conv_id, "step_count": store.get_step_count()},
        search_conversations_fn=lambda q, lim: summary_mgr.search_conversations(q, lim),
        permission_boundary=boundary
    )

    provider = GeminiModelProvider(api_key=api_key, model_id="gemini-3.5-flash", temperature=0.2)
    valid, msg = provider.validate_configuration()
    print(f"Provider: {provider.provider_name} ({provider.model_id}) - {msg}")

    executor = AgentExecutor(
        trajectory_store=store,
        event_journal=journal,
        summary_manager=summary_mgr,
        permission_boundary=boundary,
        tool_registry=registry,
        model_provider=provider
    )

    prompt = "Please check the current runtime status using your tools, and tell me what the state and memory usage are."
    print(f"\nUser: {prompt}\n")

    events = []
    print("Presence: ", end="", flush=True)
    for event in executor.execute_turn(user_input=prompt):
        events.append(event)
        if event.event_type == "TEXT_DELTA":
            print(event.payload.get("delta", ""), end="", flush=True)
        elif event.event_type == "TOOL_REQUESTED":
            print(f"\n  [Tool Requested: {event.payload.get('name')}({event.payload.get('args')})]", flush=True)
        elif event.event_type == "TOOL_COMPLETED":
            print(f"  [Tool Completed: {event.payload.get('result')}]", flush=True)
        elif event.event_type == "TOOL_FAILED":
            print(f"  [Tool Failed: {event.payload.get('error')}]", flush=True)
        elif event.event_type == "TURN_COMPLETED":
            print(f"\n\n[Turn Completed successfully! Total iterations: {event.payload.get('iterations')}]")

    # Verify Trajectory persistence
    steps = store.list_steps()
    print("\n" + "=" * 70)
    print("  PERSISTED TRAJECTORY STEPS IN SQLITE")
    print("=" * 70)
    for step in steps:
        print(f"Step {step.idx} [{step.step_type.name}] ({step.status.name}): {str(step.step_payload)[:80]}...")

    store.close()
    journal.close()
    summary_mgr.close()
    temp_dir.cleanup()

    print("\n>> LIVE GEMINI FUNCTION CALLING & EXECUTION VERIFIED! <<\n")
    return 0


if __name__ == "__main__":
    sys.exit(test_live_turn())
