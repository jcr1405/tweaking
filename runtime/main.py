"""Main Entry Point for the Presence Runtime & Agent Engine
Supports headless daemon execution, CLI diagnostics, conversation management,
supervised MCP diagnostics, persistent artifact inspection, and interactive agent chat.
"""

import sys
import os
import time
import json
import argparse
from pathlib import Path
from typing import List, Optional

# Ensure project root is in sys.path and runtime/ directory does not shadow standard library
script_dir = Path(__file__).resolve().parent
project_root = script_dir.parent
if sys.path and sys.path[0] == str(script_dir):
    sys.path.pop(0)
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from runtime.lifecycle import PresenceRuntime, RuntimeState
from runtime.conversation import ConversationMetadata
from runtime.trajectory import TrajectoryStore
from runtime.journal import EventJournal
from runtime.summaries import GlobalSummaryManager
from runtime.reconstruction import ContextAssembler
from runtime.permissions import PermissionBoundary
from runtime.tools.registry import build_default_tools
from runtime.providers.gemini import GeminiModelProvider
from runtime.providers.fake import FakeModelProvider
from runtime.agent_executor import AgentExecutor, CancellationToken
from runtime.artifacts.store import ArtifactStore
from runtime.mcp.schemas import SchemaCache
from runtime.mcp.supervisor import MCPSupervisor


def run_cli(args: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="presence",
        description="Presence Runtime & Agent Engine: Persistent Computing Companion",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run runtime continuously as a headless background daemon",
    )
    parser.add_argument(
        "--command",
        type=str,
        help="Execute a deterministic runtime command (e.g. 'status', 'help', 'config')",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Inspect health, uptime, and memory of the runtime",
    )
    parser.add_argument(
        "--provider-check",
        action="store_true",
        help="Validates AI provider configuration (SDK and GEMINI_API_KEY)",
    )
    parser.add_argument(
        "--test-live-gemini",
        action="store_true",
        help="Runs an opt-in live integration verification against Google Gemini API",
    )
    parser.add_argument(
        "--create-conversation",
        type=str,
        nargs="?",
        const="New Conversation",
        help="Creates a new conversation with an optional title and outputs its ID",
    )
    parser.add_argument(
        "--list-conversations",
        action="store_true",
        help="Lists all conversations registered in the global summaries database",
    )
    parser.add_argument(
        "--conversation-info",
        type=str,
        help="Inspects metadata and step count of a specified conversation UUID",
    )
    parser.add_argument(
        "--chat",
        type=str,
        help="Starts an interactive agent chat session for a specified conversation UUID",
    )
    parser.add_argument(
        "--mcp-status",
        action="store_true",
        help="Displays status of all registered MCP servers",
    )
    parser.add_argument(
        "--mcp-list-tools",
        action="store_true",
        help="Lists all tools discovered across registered MCP servers",
    )
    parser.add_argument(
        "--artifact-list",
        type=str,
        help="Lists all offloaded execution artifacts for a given conversation UUID",
    )
    parser.add_argument(
        "--artifact-info",
        type=str,
        nargs=2,
        metavar=("CONV_ID", "ART_ID"),
        help="Displays detailed metadata for a specific offloaded artifact",
    )
    parser.add_argument(
        "--mutex-name",
        type=str,
        default="Local\\PresenceRuntime_SingleInstanceMutex",
        help="Named Win32 mutex for instance isolation",
    )
    parser.add_argument(
        "--context-awareness",
        action="store_true",
        help="Enable native Windows foreground window awareness (Phase 2C.1)",
    )

    parsed_args = parser.parse_args(args)

    # 1. Standalone Provider Check (Can run without starting full daemon)
    if parsed_args.provider_check:
        gemini = GeminiModelProvider()
        valid, msg = gemini.validate_configuration()
        print("=== AI Provider Health Check ===")
        print(f"Provider:   {gemini.provider_name}")
        print(f"Model ID:   {gemini.model_id}")
        print(f"Ready:      {'YES' if valid else 'NO'}")
        print(f"Details:    {msg}")
        return 0 if valid else 1

    # 2. Standalone Live Gemini Test
    if parsed_args.test_live_gemini:
        gemini = GeminiModelProvider()
        valid, msg = gemini.validate_configuration()
        if not valid:
            print(f"[Presence] Cannot run live Gemini test: {msg}", file=sys.stderr)
            return 1
        print("=== Live Gemini Integration Test ===")
        print(f"Connecting to model '{gemini.model_id}'...")
        try:
            stream = gemini.generate_stream(
                system_prompt="You are a test oracle.",
                messages=[{"role": "user", "content": "Respond with the single word PONG."}]
            )
            print("Response: ", end="", flush=True)
            for chunk in stream:
                print(chunk.text_delta, end="", flush=True)
            print("\n[Presence] Live test completed successfully.")
            return 0
        except Exception as e:
            print(f"\n[Presence] Live test error: {e}", file=sys.stderr)
            return 1

    # 3. Supervised MCP Diagnostics
    if parsed_args.mcp_status:
        cache = SchemaCache()
        supervisor = MCPSupervisor(schema_cache=cache)
        statuses = supervisor.get_all_statuses()
        print("=== Supervised MCP Servers ===")
        if not statuses:
            print("No external MCP servers currently configured.")
        for s in statuses:
            print(f"[{s.server_id}] State: {s.state.value} | PID: {s.pid} | Tools: {s.tools_count} | Memory: {s.memory_mb} MB")
        return 0

    if parsed_args.mcp_list_tools:
        cache = SchemaCache()
        print("=== Discovered MCP Tools (Cached Schemas) ===")
        cached_files = list(cache.cache_dir.glob("*.json"))
        if not cached_files:
            print("No cached MCP tool schemas found on disk.")
        else:
            for cf in cached_files:
                try:
                    with open(cf, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    print(f"- {data.get('namespaced_name')}: {data.get('description', '')} (Hash: {data.get('schema_hash', '')[:8]})")
                except Exception:
                    pass
        return 0

    # 4. Artifact Inspection
    if parsed_args.artifact_list:
        store = ArtifactStore()
        artifacts = store.list_artifacts(parsed_args.artifact_list)
        print(f"=== Artifacts for Conversation {parsed_args.artifact_list} ===")
        if not artifacts:
            print("No offloaded artifacts found.")
        for a in artifacts:
            print(f"[{a.artifact_id}] Tool: {a.tool_name} | Size: {a.byte_size:,} bytes | Lines: {a.line_count:,} | Hash: {a.sha256[:8]}")
        return 0

    if parsed_args.artifact_info:
        c_id, a_id = parsed_args.artifact_info
        store = ArtifactStore()
        meta = store.get_metadata(c_id, a_id)
        if not meta:
            print(f"[Presence] Artifact '{a_id}' not found in conversation '{c_id}'.", file=sys.stderr)
            return 1
        print(f"=== Artifact: {meta.artifact_id} ===")
        print(f"Tool:         {meta.tool_name} ({meta.tool_call_id})")
        print(f"Created:      {meta.created_at}")
        print(f"Byte Size:    {meta.byte_size:,} bytes")
        print(f"Line Count:   {meta.line_count:,} lines")
        print(f"SHA-256:      {meta.sha256}")
        print(f"File Path:    {meta.file_path}")
        print("\nPreview:")
        print(meta.preview)
        return 0

    # Start runtime lifecycle for database / command operations
    runtime = PresenceRuntime(
        mutex_name=parsed_args.mutex_name,
        enable_context_awareness=parsed_args.context_awareness,
    )
    started = runtime.start()

    if not started:
        print("[Presence] ERROR: Another instance of Presence Runtime is already running.", file=sys.stderr)
        return 2

    try:
        summary_mgr = GlobalSummaryManager()

        # 5. Create Conversation
        if parsed_args.create_conversation:
            title = parsed_args.create_conversation
            meta = ConversationMetadata(title=title)
            store = TrajectoryStore(conversation_id=meta.conversation_id)
            store.save_metadata(meta)
            summary_mgr.upsert_summary(
                conversation_id=meta.conversation_id,
                title=meta.title,
                workspace_path=meta.workspace_path
            )
            store.close()
            print(f"Created Conversation ID: {meta.conversation_id}")
            print(f"Title: {meta.title}")
            return 0

        # 6. List Conversations
        if parsed_args.list_conversations:
            print("=== Registered Conversations ===")
            cursor = summary_mgr._conn.execute(
                "SELECT conversation_id, title, step_count, status, last_modified_at FROM conversation_summaries ORDER BY last_modified_at DESC;"
            )
            rows = cursor.fetchall()
            if not rows:
                print("No conversations registered yet.")
            else:
                for r in rows:
                    print(f"[{r[0]}] {r[1]} (Steps: {r[2]}, Status: {r[3]}) - {r[4]}")
            return 0

        # 7. Conversation Info
        if parsed_args.conversation_info:
            conv_id = parsed_args.conversation_info
            rec = summary_mgr.get_summary(conv_id)
            if not rec:
                print(f"[Presence] Conversation '{conv_id}' not found.", file=sys.stderr)
                return 1
            print(f"=== Conversation: {rec['title']} ===")
            print(f"ID:          {rec['conversation_id']}")
            print(f"Steps:       {rec['step_count']}")
            print(f"Status:      {rec['status']}")
            print(f"Created:     {rec['created_at']}")
            print(f"Modified:    {rec['last_modified_at']}")
            print(f"Workspace:   {rec['workspace_path']}")
            return 0

        # 8. Interactive Agent Chat
        if parsed_args.chat:
            conv_id = parsed_args.chat
            rec = summary_mgr.get_summary(conv_id)
            if not rec:
                meta = ConversationMetadata(conversation_id=conv_id, title="Interactive Chat")
                store = TrajectoryStore(conversation_id=conv_id)
                store.save_metadata(meta)
                summary_mgr.upsert_summary(conv_id, meta.title)
            else:
                store = TrajectoryStore(conversation_id=conv_id)

            journal = EventJournal(conversation_id=conv_id)
            artifact_store = ArtifactStore()
            boundary = PermissionBoundary()
            registry = build_default_tools(
                runtime_status_fn=lambda: {
                    "state": runtime.state.name,
                    "pid": runtime.pid,
                    "memory_mb": runtime.get_memory_info().working_set_mb
                },
                conversation_info_fn=lambda: {
                    "conversation_id": conv_id,
                    "step_count": store.get_step_count()
                },
                search_conversations_fn=lambda q, lim: summary_mgr.search_conversations(q, lim),
                permission_boundary=boundary
            )

            # Check if Gemini is configured; otherwise use Fake provider
            gemini = GeminiModelProvider()
            valid, _ = gemini.validate_configuration()
            provider = gemini if valid else FakeModelProvider()

            executor = AgentExecutor(
                trajectory_store=store,
                event_journal=journal,
                summary_manager=summary_mgr,
                permission_boundary=boundary,
                tool_registry=registry,
                model_provider=provider,
                artifact_store=artifact_store
            )

            print(f"=== Interactive Chat Session: {conv_id} ===")
            print(f"Model Provider: {provider.provider_name} ({provider.model_id})")
            print("Type your message. Type 'exit' to quit.\n")

            try:
                while True:
                    try:
                        user_prompt = input("you> ").strip()
                    except (EOFError, KeyboardInterrupt):
                        break
                    if not user_prompt:
                        continue
                    if user_prompt.lower() in ("exit", "quit"):
                        break

                    token = CancellationToken()
                    print("presence> ", end="", flush=True)
                    try:
                        for event in executor.execute_turn(user_prompt, cancellation_token=token):
                            if event.event_type == "TEXT_DELTA":
                                print(event.payload.get("delta", ""), end="", flush=True)
                            elif event.event_type == "TOOL_REQUESTED":
                                print(f"\n  [Tool Requested: {event.payload.get('name')}]", flush=True)
                            elif event.event_type == "TOOL_COMPLETED":
                                offloaded_str = f" (Artifact: {event.payload.get('artifact_id')})" if event.payload.get('offloaded') else ""
                                print(f"  [Tool Completed: {event.payload.get('name')}{offloaded_str}]", flush=True)
                            elif event.event_type == "TOOL_FAILED":
                                print(f"  [Tool Failed: {event.payload.get('name')} - {event.payload.get('error')}]", flush=True)
                            elif event.event_type == "TURN_COMPLETED":
                                print()
                    except KeyboardInterrupt:
                        token.cancel()
                        print("\n[Turn cancelled by user]")
            finally:
                store.close()
                journal.close()
            return 0

        # 9. Quick Status Flag
        if parsed_args.status:
            res = runtime.commands.execute("status")
            print(res.output)
            return 0

        # 10. Direct Command Execution
        if parsed_args.command:
            res = runtime.commands.execute(parsed_args.command)
            print(res.output)
            return 0 if res.success else 1

        # 11. Headless Daemon Loop
        if parsed_args.headless:
            print(f"[Presence] Runtime daemon running (PID: {runtime.pid}). Press Ctrl+C to terminate.")
            try:
                while runtime.is_running:
                    runtime.wait_for_shutdown(timeout=1.0)
            except KeyboardInterrupt:
                print("\n[Presence] KeyboardInterrupt received. Stopping...")
            return 0

        # 12. Default Interactive Console Mode
        print("=== Intelligent Presence Runtime (Backend Phase 2B) ===")
        print("Type commands (e.g. 'status', 'help', 'config', 'shutdown'). Type 'exit' to quit.\n")
        try:
            while runtime.is_running:
                try:
                    cmd_line = input("presence> ").strip()
                except EOFError:
                    break
                if not cmd_line:
                    continue
                if cmd_line.lower() in ("exit", "quit"):
                    break

                res = runtime.commands.execute(cmd_line)
                print(res.output)
                if not runtime.is_running:
                    break
        except KeyboardInterrupt:
            print("\n[Presence] Exiting interactive console...")

        return 0

    finally:
        runtime.stop()


if __name__ == "__main__":
    sys.exit(run_cli())
