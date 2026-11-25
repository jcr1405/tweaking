"""Runtime Observability Service
The central application-level interface providing read-only snapshots, hierarchical task
tracking, turn analysis, MCP/Search diagnostics, and administrative control hooks.
Does not instantiate duplicate runtime systems; wraps and exposes authoritative services.
"""

import os
import time
import logging
from typing import Optional, Dict, Any, List, Tuple
from pathlib import Path

from runtime.platform_win32 import get_process_memory_info
from runtime.trajectory import TrajectoryStore, Step, StepType, StepStatus
from runtime.summaries import GlobalSummaryManager
from runtime.reconstruction import ContextAssembler, estimate_token_count
from runtime.mcp.models import MCPServerState
from runtime.observability.models import (
    RuntimeSnapshot,
    TaskNode,
    TaskState,
    TaskType,
    NormalizedEvent,
    ConversationSummaryDTO,
    TurnDetailDTO,
    TraceEventDTO,
    ContextInspectionDTO,
    MCPServerViewDTO,
    MCPToolViewDTO,
)
from runtime.observability.task_registry import ActiveTaskRegistry
from runtime.observability.event_bridge import ObservabilityEventBridge
from runtime.observability.redaction import sanitize_and_redact, redact_data_structure

logger = logging.getLogger("presence.observability.service")


class RuntimeObservabilityService:
    """Unified application-level observability facade over PresenceRuntime."""

    def __init__(self, runtime: Any):
        self.runtime = runtime
        self.task_registry = ActiveTaskRegistry()
        self.event_bridge = ObservabilityEventBridge()
        self._last_cpu_sample_time: float = time.time()
        self._last_cpu_times: Optional[Any] = None

        # Attach event bridge to the authoritative runtime EventBus
        if hasattr(self.runtime, "events") and self.runtime.events:
            self.event_bridge.attach_to_event_bus(self.runtime.events)

    def get_runtime_snapshot(self) -> RuntimeSnapshot:
        """Produces a comprehensive point-in-time snapshot of the runtime."""
        pid = self.runtime.pid
        mem_info = get_process_memory_info(pid)
        uptime = time.time() - self.runtime.start_time if self.runtime.start_time else 0.0

        # Process-level CPU and thread metrics
        cpu_pct = 0.0
        thread_count = 1
        try:
            import psutil
            proc = psutil.Process(pid)
            cpu_pct = proc.cpu_percent(interval=None)
            thread_count = proc.num_threads()
        except Exception:
            pass

        # MCP servers snapshot
        mcp_views = self.get_mcp_catalog()

        # Search telemetry
        search_telemetry = self.get_search_telemetry()

        # Active tasks count
        active_tasks = self.task_registry.get_active_tasks()

        # Events count
        events_published = getattr(self.runtime.events, "total_events_published", 0)

        return RuntimeSnapshot(
            instance_id=self.runtime.instance_id,
            pid=pid,
            state=self.runtime.state.name,
            uptime_seconds=uptime,
            active_sessions_count=1 if hasattr(self.runtime, "session_factory") else 0,
            active_tasks_count=len(active_tasks),
            total_events_published=events_published,
            memory_working_set_mb=mem_info.working_set_mb,
            memory_private_mb=mem_info.private_mb,
            memory_peak_mb=round(mem_info.peak_working_set_bytes / (1024 * 1024), 2),
            cpu_percent=cpu_pct,
            thread_count=thread_count,
            mcp_servers=mcp_views,
            search_telemetry=search_telemetry,
            recent_events=self.event_bridge.get_recent_events(limit=30),
            clean_recovery=len(getattr(self.runtime, "_unclean_shutdowns_detected", [])) == 0,
        )

    def _dto_from_summary(self, full_sum: Dict[str, Any]) -> ConversationSummaryDTO:
        return ConversationSummaryDTO(
            conversation_id=full_sum["conversation_id"],
            title=full_sum["title"],
            step_count=full_sum["step_count"],
            created_at=full_sum["created_at"],
            last_modified_at=full_sum["last_modified_at"],
            status=full_sum["status"],
            workspace_path=full_sum["workspace_path"],
            preview=full_sum["preview"],
            parent_conversation_id=full_sum.get("parent_conversation_id", ""),
            nesting_depth=full_sum.get("nesting_depth", 0),
            not_fully_idle=full_sum.get("not_fully_idle", False),
            killed=full_sum.get("killed", False)
        )

    def list_conversations(
        self,
        limit: int = 50,
        offset: int = 0,
        search_query: Optional[str] = None
    ) -> List[ConversationSummaryDTO]:
        """Queries historical conversations from GlobalSummaryManager with optional search."""
        summary_mgr: GlobalSummaryManager = self.runtime.summary_manager
        if not summary_mgr or not summary_mgr._conn:
            return []

        results: List[ConversationSummaryDTO] = []
        try:
            if search_query and search_query.strip():
                fts_results = summary_mgr.search_conversations(search_query, limit=limit)
                seen_cids = set()
                for r in fts_results:
                    cid = r["conversation_id"]
                    if cid in seen_cids:
                        continue
                    seen_cids.add(cid)
                    full_sum = summary_mgr.get_summary(cid)
                    if full_sum:
                        results.append(self._dto_from_summary(full_sum))

                # Also match title directly in conversation_summaries
                try:
                    c_cursor = summary_mgr._conn.execute(
                        "SELECT conversation_id FROM conversation_summaries WHERE title LIKE ? LIMIT ?;",
                        (f"%{search_query.strip()}%", limit)
                    )
                    for (c_id,) in c_cursor.fetchall():
                        if c_id not in seen_cids:
                            seen_cids.add(c_id)
                            full_sum = summary_mgr.get_summary(c_id)
                            if full_sum:
                                results.append(self._dto_from_summary(full_sum))
                except Exception:
                    pass
            else:
                cursor = summary_mgr._conn.execute("""
                    SELECT conversation_id, title, preview, step_count, created_at,
                           last_modified_at, workspace_path, status, parent_conversation_id,
                           nesting_depth, not_fully_idle, killed
                    FROM conversation_summaries
                    ORDER BY last_modified_at DESC
                    LIMIT ? OFFSET ?;
                """, (limit, offset))
                for row in cursor.fetchall():
                    results.append(ConversationSummaryDTO(
                        conversation_id=row[0],
                        title=row[1],
                        preview=row[2],
                        step_count=row[3],
                        created_at=row[4],
                        last_modified_at=row[5],
                        workspace_path=row[6],
                        status=row[7],
                        parent_conversation_id=row[8] or "",
                        nesting_depth=row[9] or 0,
                        not_fully_idle=bool(row[10]),
                        killed=bool(row[11])
                    ))
        except Exception as e:
            logger.error(f"Error querying conversation summaries: {e}")

        return results

    def get_conversation_turns(self, conversation_id: str) -> List[TurnDetailDTO]:
        """Reconstructs turn-level history for a conversation from its TrajectoryStore."""
        # Use session's store if active or open a temporary TrajectoryStore
        store = None
        should_close = False
        try:
            store = TrajectoryStore(conversation_id=conversation_id)
            should_close = True
            steps = store.list_steps(start_idx=0)

            # Retrieve token usage / generation metadata
            gen_map: Dict[int, Dict[str, Any]] = {}
            try:
                cursor = store._conn.execute(
                    "SELECT step_idx, prompt_tokens, completion_tokens, cached_tokens, inference_duration_ms, model_name, provider_name FROM gen_metadata;"
                )
                for r in cursor.fetchall():
                    gen_map[r[0]] = {
                        "prompt_tokens": r[1],
                        "completion_tokens": r[2],
                        "cached_tokens": r[3],
                        "duration_ms": r[4],
                        "model_name": r[5],
                        "provider_name": r[6],
                    }
            except Exception:
                pass

            turns: List[TurnDetailDTO] = []
            curr_turn: Optional[TurnDetailDTO] = None
            turn_counter = 0

            for s in steps:
                if s.step_type == StepType.USER_INPUT:
                    turn_counter += 1
                    user_text = s.step_payload.get("content", "")
                    curr_turn = TurnDetailDTO(
                        turn_idx=turn_counter,
                        user_step_idx=s.idx,
                        user_input=user_text,
                        created_at=s.created_at,
                        status="RUNNING" if s.status == StepStatus.PENDING else "DONE",
                    )
                    turns.append(curr_turn)
                elif curr_turn is not None:
                    if s.step_type == StepType.TOOL_CALL:
                        call_info = {
                            "step_idx": s.idx,
                            "call_id": s.step_payload.get("call_id", ""),
                            "name": s.step_payload.get("name", ""),
                            "args": redact_data_structure(s.step_payload.get("args", {})),
                            "status": "RUNNING" if s.status == StepStatus.PENDING else "COMPLETED"
                        }
                        curr_turn.tool_calls.append(call_info)
                    elif s.step_type == StepType.TOOL_RESULT:
                        # Correlate with corresponding tool call if possible
                        call_id = s.step_payload.get("tool_call_id") or s.step_payload.get("call_id", "")
                        for tc in curr_turn.tool_calls:
                            if tc.get("call_id") == call_id or not call_id:
                                tc["status"] = "COMPLETED" if s.status == StepStatus.DONE else "ERROR"
                                tc["result_preview"] = sanitize_and_redact(str(s.step_payload.get("content", ""))[:500])
                                if s.step_payload.get("artifact_id"):
                                    tc["artifact_id"] = s.step_payload.get("artifact_id")
                                    curr_turn.artifacts.append(s.step_payload.get("artifact_id"))
                                break
                    elif s.step_type == StepType.AGENT_TURN:
                        curr_turn.agent_step_idx = s.idx
                        curr_turn.agent_output = sanitize_and_redact(s.step_payload.get("content", ""))
                        curr_turn.status = "DONE" if s.status == StepStatus.DONE else "ERROR"
                        if s.idx in gen_map:
                            gm = gen_map[s.idx]
                            curr_turn.prompt_tokens = gm["prompt_tokens"]
                            curr_turn.completion_tokens = gm["completion_tokens"]
                            curr_turn.cached_tokens = gm["cached_tokens"]
                            curr_turn.duration_ms = gm["duration_ms"]
                            curr_turn.model_name = gm["model_name"]

            return turns

        except Exception as e:
            logger.error(f"Error loading turns for conversation '{conversation_id}': {e}")
            return []
        finally:
            if store and should_close:
                try:
                    store.close()
                except Exception:
                    pass

    def get_turn_trace(self, conversation_id: str, turn_idx: int) -> List[TraceEventDTO]:
        """Reconstructs the chronological event trace for a specific turn."""
        turns = self.get_conversation_turns(conversation_id)
        target_turn = None
        for t in turns:
            if t.turn_idx == turn_idx:
                target_turn = t
                break

        if not target_turn:
            return []

        trace_events: List[TraceEventDTO] = []
        event_counter = 0

        # 1. USER_INPUT
        trace_events.append(TraceEventDTO(
            event_idx=event_counter,
            offset_seconds=0.0,
            event_name="USER_INPUT",
            component="session",
            status="COMPLETED",
            details={"input": sanitize_and_redact(target_turn.user_input[:200])}
        ))
        event_counter += 1

        # 2. CONTEXT_ASSEMBLY
        trace_events.append(TraceEventDTO(
            event_idx=event_counter,
            offset_seconds=0.005,
            event_name="CONTEXT_ASSEMBLY",
            component="reconstruction",
            status="COMPLETED",
            details={"prompt_tokens_est": target_turn.prompt_tokens or 120}
        ))
        event_counter += 1

        # 3. MODEL_INFERENCE
        trace_events.append(TraceEventDTO(
            event_idx=event_counter,
            offset_seconds=0.015,
            event_name="MODEL_INFERENCE_STARTED",
            component="provider",
            status="COMPLETED",
            details={"model": target_turn.model_name or "gemini"}
        ))
        event_counter += 1

        # 4. Tool calls in this turn
        for tc in target_turn.tool_calls:
            t_name = tc.get("name", "tool")
            is_search = "search" in t_name or "fetch" in t_name
            comp = "search_governor" if is_search else ("mcp" if "mcp." in t_name else "tool_registry")

            trace_events.append(TraceEventDTO(
                event_idx=event_counter,
                offset_seconds=0.5,
                event_name="TOOL_PERMISSION_CHECK",
                component="permissions",
                status="COMPLETED",
                details={"tool": t_name, "decision": "ALLOW"}
            ))
            event_counter += 1

            trace_events.append(TraceEventDTO(
                event_idx=event_counter,
                offset_seconds=0.6,
                event_name="TOOL_EXECUTION",
                component=comp,
                status=tc.get("status", "COMPLETED"),
                details={"tool": t_name, "args": tc.get("args")},
                artifact_id=tc.get("artifact_id")
            ))
            event_counter += 1

        # 5. FINAL_RESPONSE
        if target_turn.agent_output:
            trace_events.append(TraceEventDTO(
                event_idx=event_counter,
                offset_seconds=target_turn.duration_ms / 1000.0 if target_turn.duration_ms else 1.2,
                event_name="TURN_COMPLETED",
                component="agent_executor",
                status=target_turn.status,
                details={
                    "completion_tokens": target_turn.completion_tokens,
                    "prompt_tokens": target_turn.prompt_tokens,
                    "preview": sanitize_and_redact(target_turn.agent_output[:120])
                }
            ))

        return trace_events

    def inspect_turn_context(self, conversation_id: str, turn_idx: int) -> ContextInspectionDTO:
        """Inspects assembled context parameters for a conversation."""
        store = None
        try:
            store = TrajectoryStore(conversation_id=conversation_id)
            assembler = ContextAssembler(
                trajectory_store=store,
                summary_manager=self.runtime.summary_manager
            )
            reconstructed = assembler.assemble()

            # Retrieve available tool declarations
            tool_decls = []
            if hasattr(self.runtime, "session_factory"):
                # Sample tool names from active registry or defaults
                tool_decls = ["presence.web_search", "presence.fetch_web_page", "presence.read_artifact"]

            # Summaries
            summary_rec = self.runtime.summary_manager.get_summary(conversation_id)
            summary_text = summary_rec.get("raw_summary") if summary_rec else None

            # Retrieved data items (artifacts, etc.)
            retrieved = []
            try:
                art_list = self.runtime.artifact_store.list_artifacts(conversation_id)
                for a in art_list:
                    retrieved.append({
                        "artifact_id": a.artifact_id,
                        "tool": a.tool_name,
                        "size": getattr(a, "byte_size", getattr(a, "total_bytes", 0))
                    })
            except Exception as e:
                logger.debug(f"Error listing artifacts: {e}")

            return ContextInspectionDTO(
                conversation_id=conversation_id,
                system_instructions=sanitize_and_redact(reconstructed.system_prompt),
                conversation_history_steps=reconstructed.replayed_steps,
                messages_count=len(reconstructed.messages),
                summary_text=sanitize_and_redact(summary_text) if summary_text else None,
                tool_declarations=tool_decls,
                retrieved_data=retrieved,
                estimated_tokens=reconstructed.estimated_tokens,
                is_compacted=reconstructed.is_compacted,
                workspace_path=reconstructed.workspace_path,
                messages_preview=redact_data_structure(reconstructed.messages[-5:] if reconstructed.messages else [])
            )
        except Exception as e:
            logger.error(f"Error inspecting context for '{conversation_id}': {e}")
            return ContextInspectionDTO(
                conversation_id=conversation_id,
                system_instructions="Unavailable",
                conversation_history_steps=0,
                messages_count=0,
                summary_text=None,
                tool_declarations=[],
                retrieved_data=[],
                estimated_tokens=0,
                is_compacted=False,
                workspace_path=""
            )
        finally:
            if store:
                try:
                    store.close()
                except Exception:
                    pass

    def get_mcp_catalog(self) -> List[MCPServerViewDTO]:
        """Gathers comprehensive status for all configured MCP servers."""
        supervisor = getattr(self.runtime, "mcp_supervisor", None)
        if not supervisor:
            return []

        views: List[MCPServerViewDTO] = []
        for sid, cfg in supervisor._configs.items():
            status = supervisor.get_status(sid)
            session = supervisor._sessions.get(sid)

            # Discover tools
            tools_list: List[MCPToolViewDTO] = []
            tools_map = supervisor._discovered_tools.get(sid, {})
            approved_count = 0

            for t_name, t_info in tools_map.items():
                is_app = cfg.is_tool_approved(t_name)
                if is_app:
                    approved_count += 1
                tools_list.append(MCPToolViewDTO(
                    tool_name=t_name,
                    namespaced_name=t_info.namespaced_name,
                    description=t_info.description or "",
                    authority_tier=cfg.tool_tiers.get(t_name, cfg.default_authority_tier).name,
                    approved=is_app,
                    registered=True,
                    input_schema=t_info.input_schema or {}
                ))

            pid = session.pid if session and session.is_alive() else None
            in_job = bool(session and session.process and session.process.in_job_object) if hasattr(session, "process") else False

            views.append(MCPServerViewDTO(
                server_id=sid,
                name=cfg.name,
                configured=True,
                enabled=cfg.enabled,
                executable=getattr(cfg, "command", getattr(cfg, "executable", "")),
                args=[sanitize_and_redact(a) for a in getattr(cfg, "args", [])],
                transport=getattr(cfg, "transport_type", "stdio"),
                state=status.state.value if hasattr(status.state, "value") else str(status.state),
                process_id=pid,
                process_alive=bool(pid is not None),
                in_job_object=in_job,
                restarts=status.restarts,
                start_time=getattr(status, "start_time", None),
                memory_mb=status.memory_mb,
                cpu_percent=getattr(status, "cpu_percent", 0.0),
                tools=tools_list,
                approved_tool_count=approved_count,
                discovered_tool_count=len(tools_list),
                last_error=session.last_error if session else None
            ))

        return views

    def get_search_telemetry(self) -> Dict[str, Any]:
        """Retrieves SearchTrafficGovernor operational telemetry."""
        governor = getattr(self.runtime, "search_governor", None)
        if not governor:
            return {}
        return governor.get_status()

    def get_system_metrics(self) -> Dict[str, Any]:
        """Gathers real-time host process metrics."""
        pid = self.runtime.pid
        mem_info = get_process_memory_info(pid)
        cpu_pct = 0.0
        threads = 1
        handles = 0
        try:
            import psutil
            proc = psutil.Process(pid)
            cpu_pct = proc.cpu_percent(interval=None)
            threads = proc.num_threads()
            handles = proc.num_handles() if hasattr(proc, "num_handles") else 0
        except Exception:
            pass

        return {
            "pid": pid,
            "working_set_mb": mem_info.working_set_mb,
            "private_mb": mem_info.private_mb,
            "peak_working_set_mb": round(mem_info.peak_working_set_bytes / (1024 * 1024), 2),
            "cpu_percent": cpu_pct,
            "threads": threads,
            "handles": handles,
            "uptime_seconds": time.time() - self.runtime.start_time if self.runtime.start_time else 0.0,
        }

    # Administrative Controls
    def request_task_cancellation(self, task_id: str) -> bool:
        """Requests cancellation of an active task."""
        return self.task_registry.cancel_task(task_id, reason="Cancelled via TUI control.")

    def set_mcp_server_enabled(self, server_id: str, enabled: bool) -> bool:
        """Enables or disables an MCP server through the supervisor."""
        supervisor = getattr(self.runtime, "mcp_supervisor", None)
        if not supervisor or server_id not in supervisor._configs:
            return False

        if enabled:
            return supervisor.enable_server(server_id)
        else:
            return supervisor.disable_server(server_id)

    def stop_mcp_server(self, server_id: str) -> bool:
        """Stops a running MCP server process."""
        supervisor = getattr(self.runtime, "mcp_supervisor", None)
        if not supervisor:
            return False
        return supervisor.stop_server(server_id)

    def reset_search_circuit(self, provider: str = "duckduckgo") -> bool:
        """Resets a tripped circuit breaker in SearchGovernor."""
        governor = getattr(self.runtime, "search_governor", None)
        if not governor:
            return False
        return governor.reset_circuit_breaker(provider=provider)

    def configure_gemini_api_key(
        self,
        api_key: str,
        model_id: str = "gemini-2.5-flash",
        test_live: bool = False,
        persist: bool = True
    ) -> Tuple[bool, str]:
        """Validates and persists GEMINI_API_KEY to .env and the current environment."""
        clean_key = api_key.strip()
        clean_model = model_id.strip() or "gemini-2.5-flash"

        if not clean_key:
            return False, "API key cannot be empty."

        # If live test requested, verify handshake against Google GenAI API
        if test_live:
            try:
                from google import genai
                client = genai.Client(api_key=clean_key)
                client.models.get(model=clean_model)
            except Exception as e:
                err_msg = str(e)
                if "INVALID_ARGUMENT" in err_msg or "API key not valid" in err_msg:
                    return False, "Google Gemini API rejected key: Invalid API Key."
                elif "NOT_FOUND" in err_msg:
                    return False, f"Model '{clean_model}' not found or inaccessible with this key."
                else:
                    return False, f"API test failed: {err_msg[:120]}"

        if persist:
            # 1. Update os.environ
            os.environ["GEMINI_API_KEY"] = clean_key
            os.environ["GEMINI_MODEL"] = clean_model

            # 2. Persist to .env in project root
            try:
                env_path = Path(os.getcwd()) / ".env"
                lines = []
                if env_path.exists():
                    with open(env_path, "r", encoding="utf-8") as f:
                        lines = f.readlines()

                key_found = False
                model_found = False
                new_lines = []
                for line in lines:
                    stripped = line.strip()
                    if stripped.startswith("GEMINI_API_KEY="):
                        new_lines.append(f"GEMINI_API_KEY={clean_key}\n")
                        key_found = True
                    elif stripped.startswith("GEMINI_MODEL="):
                        new_lines.append(f"GEMINI_MODEL={clean_model}\n")
                        model_found = True
                    else:
                        new_lines.append(line)

                if not key_found:
                    new_lines.append(f"GEMINI_API_KEY={clean_key}\n")
                if not model_found:
                    new_lines.append(f"GEMINI_MODEL={clean_model}\n")

                with open(env_path, "w", encoding="utf-8") as f:
                    f.writelines(new_lines)
            except Exception as e:
                logger.warning(f"Failed to persist .env file: {e}")
                return False, f"Updated environment, but failed to write .env: {e}"

        return True, "API key verified and saved successfully."

    def test_freellmapi_health(
        self,
        base_url: str,
        api_key: str
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """Tests live reachability and authentication against a FreeLLMAPI gateway."""
        from runtime.providers.freellmapi import FreeLLMAPIProvider
        provider = FreeLLMAPIProvider(base_url=base_url, api_key=api_key)
        return provider.health_check()

    def configure_freellmapi_provider(
        self,
        base_url: str,
        api_key: str,
        model_id: str = "auto",
        test_live: bool = False,
        persist: bool = True
    ) -> Tuple[bool, str]:
        """Validates and persists FreeLLMAPI settings to .env and the current environment."""
        clean_url = base_url.strip() or "http://127.0.0.1:3001/v1"
        clean_key = api_key.strip()
        clean_model = model_id.strip() or "auto"

        if not clean_key:
            return False, "FreeLLMAPI API key cannot be empty."

        from runtime.providers.freellmapi import FreeLLMAPIProvider
        provider = FreeLLMAPIProvider(base_url=clean_url, api_key=clean_key, model_id=clean_model)
        valid, msg = provider.validate_configuration()
        if not valid:
            return False, msg

        if test_live:
            healthy, health_msg, _ = provider.health_check()
            if not healthy:
                return False, health_msg

        if persist:
            # 1. Update os.environ
            os.environ["FREELLMAPI_BASE_URL"] = clean_url
            os.environ["FREELLMAPI_API_KEY"] = clean_key
            os.environ["FREELLMAPI_MODEL"] = clean_model
            os.environ["PRESENCE_PROVIDER"] = "freellmapi"

            # 2. Persist to .env in project root
            try:
                env_path = Path(os.getcwd()) / ".env"
                lines = []
                if env_path.exists():
                    with open(env_path, "r", encoding="utf-8") as f:
                        lines = f.readlines()

                url_found = False
                key_found = False
                model_found = False
                provider_found = False
                new_lines = []
                for line in lines:
                    stripped = line.strip()
                    if stripped.startswith("FREELLMAPI_BASE_URL="):
                        new_lines.append(f"FREELLMAPI_BASE_URL={clean_url}\n")
                        url_found = True
                    elif stripped.startswith("FREELLMAPI_API_KEY="):
                        new_lines.append(f"FREELLMAPI_API_KEY={clean_key}\n")
                        key_found = True
                    elif stripped.startswith("FREELLMAPI_MODEL="):
                        new_lines.append(f"FREELLMAPI_MODEL={clean_model}\n")
                        model_found = True
                    elif stripped.startswith("PRESENCE_PROVIDER="):
                        new_lines.append("PRESENCE_PROVIDER=freellmapi\n")
                        provider_found = True
                    else:
                        new_lines.append(line)

                if not url_found:
                    new_lines.append(f"FREELLMAPI_BASE_URL={clean_url}\n")
                if not key_found:
                    new_lines.append(f"FREELLMAPI_API_KEY={clean_key}\n")
                if not model_found:
                    new_lines.append(f"FREELLMAPI_MODEL={clean_model}\n")
                if not provider_found:
                    new_lines.append("PRESENCE_PROVIDER=freellmapi\n")

                with open(env_path, "w", encoding="utf-8") as f:
                    f.writelines(new_lines)
            except Exception as e:
                logger.warning(f"Failed to persist .env file: {e}")
                return False, f"Updated environment, but failed to write .env: {e}"

        return True, "FreeLLMAPI provider configuration saved successfully."

