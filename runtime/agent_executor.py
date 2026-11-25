"""Single-Agent Execution Engine
Coordinates the multi-step agent turn loop: context reconstruction, model requests,
streaming, manual tool authorization and execution, and dual-journal persistence.
"""

import json
import logging
import concurrent.futures
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List, Iterator

from runtime.trajectory import TrajectoryStore, Step, StepType, StepStatus
from runtime.journal import EventJournal
from runtime.summaries import GlobalSummaryManager
from runtime.reconstruction import ContextAssembler
from runtime.permissions import PermissionBoundary, PermissionTier
from runtime.tools.registry import ToolRegistry, ToolDefinition
from runtime.providers.base import IModelProvider, ModelStreamChunk
from runtime.artifacts.store import ArtifactStore
from runtime.search.governor import active_turn_context, TurnSearchContext

logger = logging.getLogger("presence.agent_executor")


@dataclass
class CancellationToken:
    """Thread-safe cancellation flag for active model generation or tool execution."""
    is_cancelled: bool = False

    def cancel(self) -> None:
        """Sets cancellation flag."""
        self.is_cancelled = True
        logger.info("Cancellation requested on CancellationToken.")


@dataclass
class AgentExecutionEvent:
    """Normalized event emitted during an agent turn."""
    event_type: str
    payload: Dict[str, Any] = field(default_factory=dict)
    step_index: int = -1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_type": self.event_type,
            "step_index": self.step_index,
            "payload": self.payload
        }


class AgentExecutor:
    """Orchestrates single-agent turn execution with manual tool authorization."""

    def __init__(
        self,
        trajectory_store: TrajectoryStore,
        event_journal: EventJournal,
        summary_manager: GlobalSummaryManager,
        permission_boundary: PermissionBoundary,
        tool_registry: ToolRegistry,
        model_provider: IModelProvider,
        context_assembler: Optional[ContextAssembler] = None,
        artifact_store: Optional[ArtifactStore] = None,
        task_registry: Optional[Any] = None,
        event_bridge: Optional[Any] = None,
    ):
        self.store = trajectory_store
        self.journal = event_journal
        self.summary_manager = summary_manager
        self.permission_boundary = permission_boundary
        self.tool_registry = tool_registry
        self.provider = model_provider
        self.assembler = context_assembler or ContextAssembler(
            trajectory_store=self.store,
            summary_manager=self.summary_manager
        )
        self.artifact_store = artifact_store or ArtifactStore()
        self.task_registry = task_registry
        self.event_bridge = event_bridge

        # Ensure presence.read_artifact is registered into tool registry
        self._ensure_artifact_tool_registered()

    def _ensure_artifact_tool_registered(self) -> None:
        """Registers presence.read_artifact into the tool registry if not already present."""
        if not self.tool_registry.get_tool("presence.read_artifact"):
            self.tool_registry.register(ToolDefinition(
                tool_id="presence.read_artifact",
                name="read_artifact",
                description="Reads chunks or line ranges from an offloaded large tool execution artifact stored on disk.",
                parameters={
                    "type": "object",
                    "properties": {
                        "artifact_id": {
                            "type": "string",
                            "description": "Identifier of the artifact to read (e.g. 'art_...')."
                        },
                        "offset": {
                            "type": "integer",
                            "description": "Starting line number or byte offset (0-indexed, default 0)."
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of lines or bytes to return (default 100 lines or 4000 bytes)."
                        },
                        "mode": {
                            "type": "string",
                            "enum": ["lines", "bytes"],
                            "description": "Read mode: 'lines' to read by line count (default), or 'bytes' to read by byte offset."
                        }
                    },
                    "required": ["artifact_id"],
                    "additionalProperties": False
                },
                authority_tier=PermissionTier.L0_OBSERVE,
                handler=lambda args: self.artifact_store.read_artifact(
                    conversation_id=self.store.conversation_id,
                    artifact_id=args.get("artifact_id", ""),
                    offset=int(args.get("offset", 0)),
                    limit=int(args.get("limit", 100)),
                    mode=args.get("mode", "lines")
                ).to_dict()
            ), permission_boundary=self.permission_boundary)

    def execute_turn(
        self,
        user_input: str,
        system_prompt: str = "You are a persistent intelligent desktop presence.",
        desktop_ambient_context: Optional[Dict[str, Any]] = None,
        cancellation_token: Optional[CancellationToken] = None,
        max_tool_iterations: Optional[int] = None,
        request_config: Optional[Any] = None
    ) -> Iterator[AgentExecutionEvent]:
        """Executes a full turn, yielding normalized streaming events."""
        token = cancellation_token or CancellationToken()

        if token.is_cancelled:
            yield AgentExecutionEvent(
                event_type="TURN_CANCELLED",
                payload={"reason": "Turn cancelled before execution."}
            )
            return

        if not system_prompt or system_prompt == "You are a persistent intelligent desktop presence.":
            from datetime import datetime
            now_str = datetime.now().strftime("%A, %B %d, %Y")
            system_prompt = (
                f"You are Presence, a persistent, local-first personal computer companion running natively on Windows.\n"
                f"Current Date: {now_str}.\n"
                f"You understand the user's computer, learn how they operate it, assist with difficult desktop interactions, "
                f"maintain environmental awareness, and execute approved computer-use workflows in the user's preferred way.\n"
                f"You do not generate code, execute build systems, or act as an autonomous software developer unless explicitly instructed.\n"
                f"You have access to tools including real-time web search (presence.web_search), web page inspection (presence.fetch_web_page), "
                f"and local operational delegation (presence.delegate_local_work)."
            )
            if self.tool_registry.get_tool("presence.invoke_subagent"):
                system_prompt += (
                    "\n\nSubagent Delegation Guidelines:\n"
                    "For research tasks requiring multiple sequential web searches, site inspections, broad comparisons, "
                    "or deep file audits, delegate the work to an ephemeral subagent using 'presence.invoke_subagent' "
                    "(role='researcher') instead of running many tool iterations in this main conversation. "
                    "The subagent will perform the investigation in an isolated session and return a single distilled summary."
                )

        if desktop_ambient_context and desktop_ambient_context.get("task_notifications"):
            notes_lines = []
            for n in desktop_ambient_context["task_notifications"]:
                p = n.get("payload", {})
                t_id = p.get("task_id", "unknown")
                d_name = p.get("display_name", t_id)
                e_type = n.get("event_type", "TASK_UPDATE")
                art = f" (Artifact: {p.get('artifact_id')})" if p.get('artifact_id') else ""
                notes_lines.append(f"- [{e_type}] Task '{t_id}' ({d_name}){art}")
            system_prompt += "\n\nReactive Background Task Updates:\n" + "\n".join(notes_lines)

        # Unconstrained turn search and fetch budget
        turn_search_ctx = TurnSearchContext(max_searches=None, max_fetches=None)
        turn_ctx_token = active_turn_context.set(turn_search_ctx)

        # 1. Commit and record USER_INPUT step (idempotent if already recorded by session preemption)
        cnt = self.store.get_step_count()
        last_step = self.store.get_step(cnt - 1) if cnt > 0 else None
        if last_step and last_step.step_type == StepType.USER_INPUT and last_step.step_payload.get("content") == user_input:
            user_idx = last_step.idx
        else:
            user_idx = self.store.get_next_step_index()
            user_step = Step(
                idx=user_idx,
                step_type=StepType.USER_INPUT,
                status=StepStatus.DONE,
                step_payload={"content": user_input}
            )
            self.store.append_step(user_step)
            self.journal.append_step_event(user_step)

        turn_task = None
        if self.task_registry:
            try:
                from runtime.observability.models import TaskType
                turn_task = self.task_registry.register_task(
                    task_type=TaskType.TURN,
                    display_name=f"Turn #{user_idx}: {user_input[:40]}",
                    conversation_id=self.store.conversation_id,
                    cancellable=True,
                    cancellation_hook=token.cancel
                )
                self.task_registry.start_task(turn_task.task_id)
            except Exception as e:
                logger.debug(f"Error registering turn task: {e}")

        def _emit(ev: AgentExecutionEvent) -> AgentExecutionEvent:
            if self.event_bridge:
                try:
                    self.event_bridge.emit_synthetic(
                        event_type=ev.event_type,
                        payload=ev.payload,
                        component="agent_executor",
                        conversation_id=self.store.conversation_id,
                        turn_idx=user_idx,
                        task_id=turn_task.task_id if turn_task else None,
                        level="ERROR" if "FAIL" in ev.event_type else "INFO"
                    )
                except Exception:
                    pass
            return ev

        # Index user message into FTS5
        meta = self.store.get_metadata()
        title = meta.title if meta else "Active Conversation"
        workspace = meta.workspace_path if meta else ""
        self.summary_manager.index_step_text(
            conversation_id=self.store.conversation_id,
            step_idx=user_idx,
            title=title,
            workspace_path=workspace,
            user_body=user_input
        )

        yield _emit(AgentExecutionEvent(
            event_type="TURN_STARTED",
            step_index=user_idx,
            payload={"user_input": user_input}
        ))

        # Update running state
        self.summary_manager.update_idle_state(self.store.conversation_id, not_fully_idle=True)

        if hasattr(self.provider, "start_turn"):
            try:
                self.provider.start_turn(conversation_id=self.store.conversation_id)
            except TypeError:
                self.provider.start_turn()
            except Exception:
                pass

        iterations = 0
        try:
            while max_tool_iterations is None or iterations < max_tool_iterations:
                iterations += 1

                if token.is_cancelled:
                    logger.info("Agent execution cancelled between iterations.")
                    yield _emit(AgentExecutionEvent(
                        event_type="TURN_CANCELLED",
                        payload={"reason": "Cancelled by user token."}
                    ))
                    break

                # 2. Reconstruct context deterministically from SQLite
                reconstructed = self.assembler.assemble(
                    system_instructions=system_prompt,
                    desktop_ambient_context=desktop_ambient_context
                )

                # 3. Retrieve available tool schemas
                tool_declarations = self.tool_registry.get_declarations()

                yield _emit(AgentExecutionEvent(
                    event_type="MODEL_REQUESTED",
                    payload={
                        "iteration": iterations,
                        "model": self.provider.model_id,
                        "reconstructed_steps": reconstructed.replayed_steps
                    }
                ))

                # 4. Stream model response
                accumulated_text = ""
                extracted_tool_calls: List[Dict[str, Any]] = []
                last_usage: Optional[Dict[str, int]] = None

                try:
                    kwargs = {
                        "system_prompt": reconstructed.system_prompt,
                        "messages": reconstructed.messages,
                        "tools": tool_declarations if tool_declarations else None,
                        "cancellation_token": token,
                    }
                    if hasattr(self.provider, "_primary_provider"):
                        from runtime.providers.coordinator import InferenceRequestConfig
                        kwargs["request_config"] = request_config or InferenceRequestConfig(
                            purpose="tool_execution",
                            cancellation_token=token,
                            conversation_id=self.store.conversation_id
                        )
                    elif request_config is not None:
                        kwargs["request_config"] = request_config
                    stream_iter = self.provider.generate_stream(**kwargs)

                    for chunk in stream_iter:
                        if token.is_cancelled:
                            yield _emit(AgentExecutionEvent(
                                event_type="TURN_CANCELLED",
                                payload={"reason": "Cancelled during model streaming."}
                            ))
                            return

                        if getattr(chunk, "finish_reason", None) == "PROVIDER_RESET":
                            accumulated_text = ""
                            extracted_tool_calls.clear()
                            yield _emit(AgentExecutionEvent(
                                event_type="TEXT_RESET",
                                payload={"reason": chunk.thinking_status}
                            ))
                            continue

                        if chunk.thinking_status:
                            yield _emit(AgentExecutionEvent(
                                event_type="THINKING_DELTA",
                                payload={"thinking": chunk.thinking_status}
                            ))

                        if chunk.text_delta:
                            accumulated_text += chunk.text_delta
                            yield _emit(AgentExecutionEvent(
                                event_type="TEXT_DELTA",
                                payload={"delta": chunk.text_delta}
                            ))

                        if chunk.tool_calls:
                            extracted_tool_calls.extend(chunk.tool_calls)

                        if chunk.usage:
                            last_usage = chunk.usage
                            yield _emit(AgentExecutionEvent(
                                event_type="USAGE_UPDATED",
                                payload=chunk.usage
                            ))

                except Exception as model_err:
                    err_msg = str(model_err)
                    logger.error(f"Model generation failure: {err_msg}")
                    yield _emit(AgentExecutionEvent(
                        event_type="TURN_FAILED",
                        payload={"error": err_msg, "iteration": iterations}
                    ))
                    return

                # 5. Handle model tool requests vs terminal text response
                if not extracted_tool_calls:
                    # Model completed turn with text response!
                    thinking_text = ""
                    content_text = accumulated_text
                    if "</think>" in accumulated_text:
                        parts = accumulated_text.split("</think>", 1)
                        thinking_text = parts[0].replace("<think>", "").strip()
                        content_text = parts[1].strip()

                    agent_idx = self.store.get_next_step_index()
                    agent_step = Step(
                        idx=agent_idx,
                        step_type=StepType.AGENT_TURN,
                        status=StepStatus.DONE,
                        step_payload={
                            "content": content_text,
                            "thinking": thinking_text
                        }
                    )
                    self.store.append_step(agent_step)
                    self.journal.append_step_event(agent_step)

                    # Record generation telemetry if usage was provided
                    if last_usage:
                        self.store.record_generation_metadata(
                            step_idx=agent_idx,
                            prompt_tokens=last_usage.get("prompt_tokens", 0),
                            completion_tokens=last_usage.get("completion_tokens", 0),
                            cached_tokens=last_usage.get("cached_tokens", 0),
                            duration_ms=0.0,
                            model_name=self.provider.model_id,
                            provider_name=self.provider.provider_name
                        )

                    # Index agent response in FTS5
                    self.summary_manager.index_step_text(
                        conversation_id=self.store.conversation_id,
                        step_idx=agent_idx,
                        title=title,
                        workspace_path=workspace,
                        agent_body=content_text
                    )

                    yield _emit(AgentExecutionEvent(
                        event_type="TURN_COMPLETED",
                        step_index=agent_idx,
                        payload={
                            "final_text": content_text,
                            "thinking": thinking_text,
                            "iterations": iterations,
                            "usage": last_usage
                        }
                    ))
                    break

                # Model requested tool call(s) - Partition into concurrent batches & serial barriers (Phase 3)
                tool_batches: List[List[Dict[str, Any]]] = []
                current_batch: List[Dict[str, Any]] = []

                for tc in extracted_tool_calls:
                    t_name = tc.get("name", "")
                    t_def = self.tool_registry.get_tool(t_name)
                    is_ro = bool(t_def and getattr(t_def, "read_only", False))
                    if is_ro:
                        current_batch.append(tc)
                    else:
                        if current_batch:
                            tool_batches.append(current_batch)
                            current_batch = []
                        tool_batches.append([tc])
                if current_batch:
                    tool_batches.append(current_batch)

                for batch in tool_batches:
                    if token.is_cancelled:
                        yield _emit(AgentExecutionEvent(
                            event_type="TURN_CANCELLED",
                            payload={"reason": "Cancelled before tool execution."}
                        ))
                        return

                    # 1. Register, emit TOOL_REQUESTED, commit TOOL_CALL steps, and emit TOOL_STARTED in deterministic order
                    batch_metadata: List[Dict[str, Any]] = []
                    for tc in batch:
                        call_id = tc.get("call_id", f"call_{iterations}")
                        tool_name = tc.get("name", "")
                        tool_args = tc.get("args", {})

                        tool_task = None
                        if self.task_registry and turn_task:
                            try:
                                from runtime.observability.models import TaskType
                                is_search = "search" in tool_name or "fetch" in tool_name
                                t_type = TaskType.SEARCH_REQUEST if is_search else (
                                    TaskType.MCP_REQUEST if "mcp." in tool_name else TaskType.TOOL_EXECUTION
                                )
                                tool_task = self.task_registry.register_task(
                                    task_type=t_type,
                                    display_name=tool_name,
                                    parent_task_id=turn_task.task_id,
                                    conversation_id=self.store.conversation_id,
                                    metadata={"call_id": call_id, "args": tool_args}
                                )
                                self.task_registry.start_task(tool_task.task_id)
                            except Exception as e:
                                logger.debug(f"Error registering tool task: {e}")

                        yield _emit(AgentExecutionEvent(
                            event_type="TOOL_REQUESTED",
                            payload={"call_id": call_id, "name": tool_name, "args": tool_args}
                        ))

                        # Commit TOOL_CALL step
                        tc_idx = self.store.get_next_step_index()
                        tc_payload = {"call_id": call_id, "name": tool_name, "args": tool_args}
                        if tc.get("thought_signature"):
                            tc_payload["thought_signature"] = tc["thought_signature"]
                        tc_step = Step(
                            idx=tc_idx,
                            step_type=StepType.TOOL_CALL,
                            status=StepStatus.DONE,
                            step_payload=tc_payload
                        )
                        self.store.append_step(tc_step)
                        self.journal.append_step_event(tc_step)

                        # Emit TOOL_STARTED
                        yield _emit(AgentExecutionEvent(
                            event_type="TOOL_STARTED",
                            step_index=tc_idx,
                            payload={"name": tool_name}
                        ))

                        batch_metadata.append({
                            "tc": tc,
                            "call_id": call_id,
                            "tool_name": tool_name,
                            "tool_args": tool_args,
                            "tool_task": tool_task,
                            "tc_idx": tc_idx,
                        })

                    # 2. Execute tools in batch (parallel if len > 1, serial if len == 1)
                    if len(batch) > 1:
                        def _execute_worker(meta_item):
                            try:
                                from runtime.tools.delegation_tools import (
                                    current_tool_call_id,
                                    current_conversation_id,
                                    current_cancellation_token,
                                )
                                current_tool_call_id.set(meta_item["call_id"])
                                current_conversation_id.set(self.store.conversation_id)
                                current_cancellation_token.set(token)
                            except ImportError:
                                pass
                            return self.tool_registry.execute(
                                tool_id=meta_item["tool_name"],
                                args=meta_item["tool_args"],
                                permission_boundary=self.permission_boundary,
                                conversation_id=self.store.conversation_id,
                                call_id=meta_item["call_id"],
                            )

                        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(batch), 5)) as pool:
                            futures = [pool.submit(_execute_worker, m) for m in batch_metadata]
                            exec_results = [f.result() for f in futures]
                    else:
                        m0 = batch_metadata[0]
                        try:
                            from runtime.tools.delegation_tools import (
                                current_tool_call_id,
                                current_conversation_id,
                                current_cancellation_token,
                            )
                            current_tool_call_id.set(m0["call_id"])
                            current_conversation_id.set(self.store.conversation_id)
                            current_cancellation_token.set(token)
                        except ImportError:
                            pass
                        exec_results = [self.tool_registry.execute(
                            tool_id=m0["tool_name"],
                            args=m0["tool_args"],
                            permission_boundary=self.permission_boundary,
                            conversation_id=self.store.conversation_id,
                            call_id=m0["call_id"],
                        )]

                    # 3. Process and commit results in strict deterministic order
                    for meta, exec_res in zip(batch_metadata, exec_results):
                        tool_name = meta["tool_name"]
                        call_id = meta["call_id"]
                        tool_task = meta["tool_task"]

                        # Determine string representation of tool output
                        if exec_res.get("status") == "SUCCESS":
                            raw_result = exec_res.get("result", {})
                            if isinstance(raw_result, str):
                                res_content = raw_result
                            else:
                                res_content = json.dumps(raw_result, ensure_ascii=False)
                        else:
                            res_content = str(exec_res.get("error", "Error"))

                        # Check for large output offloading (> 40 KB)
                        offloaded = False
                        artifact_id = None
                        preview_content = res_content

                        if exec_res.get("status") == "SUCCESS" and self.artifact_store.should_offload(res_content):
                            try:
                                artifact_meta = self.artifact_store.store_text_artifact(
                                    conversation_id=self.store.conversation_id,
                                    tool_call_id=call_id,
                                    tool_name=tool_name,
                                    content_str=res_content
                                )
                                offloaded = True
                                artifact_id = artifact_meta.artifact_id
                                preview_content = artifact_meta.preview
                            except Exception as art_err:
                                logger.error(f"Failed to offload large artifact for tool '{tool_name}': {art_err}")

                        # Commit TOOL_RESULT step
                        tr_idx = self.store.get_next_step_index()
                        tr_payload: Dict[str, Any] = {
                            "tool_call_id": call_id,
                            "name": tool_name,
                            "content": preview_content
                        }
                        if offloaded and artifact_id:
                            tr_payload["artifact_id"] = artifact_id
                            tr_payload["byte_size"] = artifact_meta.byte_size
                            tr_payload["sha256"] = artifact_meta.sha256

                        has_sub = False
                        sub_cid = ""
                        raw_r = exec_res.get("result")
                        if isinstance(raw_r, dict):
                            sub_cid = str(raw_r.get("conversation_id", raw_r.get("subagent_conversation_id", "")))
                            if sub_cid:
                                has_sub = True

                        tr_step = Step(
                            idx=tr_idx,
                            step_type=StepType.TOOL_RESULT,
                            status=StepStatus.DONE if exec_res.get("status") == "SUCCESS" else StepStatus.ERROR,
                            step_payload=tr_payload,
                            has_subtrajectory=has_sub,
                            subagent_conversation_id=sub_cid
                        )
                        self.store.append_step(tr_step)
                        self.journal.append_step_event(tr_step)

                        # Index tool output into FTS5 for search recall
                        self.summary_manager.index_step_text(
                            conversation_id=self.store.conversation_id,
                            step_idx=tr_idx,
                            title=title,
                            workspace_path=workspace,
                            tool_content=f"{tool_name}: {preview_content[:2000]}"
                        )

                        if exec_res.get("status") == "SUCCESS":
                            if self.task_registry and tool_task:
                                self.task_registry.complete_task(tool_task.task_id, artifact_id=artifact_id)
                            yield _emit(AgentExecutionEvent(
                                event_type="TOOL_COMPLETED",
                                step_index=tr_idx,
                                payload={
                                    "name": tool_name,
                                    "result": preview_content if offloaded else exec_res.get("result"),
                                    "artifact_id": artifact_id,
                                    "offloaded": offloaded
                                }
                            ))
                        else:
                            if self.task_registry and tool_task:
                                self.task_registry.fail_task(
                                    tool_task.task_id,
                                    error_message=str(exec_res.get("error", "Error"))
                                )
                            yield _emit(AgentExecutionEvent(
                                event_type="TOOL_FAILED",
                                step_index=tr_idx,
                                payload={"name": tool_name, "error": exec_res.get("error")}
                            ))

            if max_tool_iterations is not None and iterations >= max_tool_iterations:
                logger.warning(f"Turn reached max_tool_iterations ({max_tool_iterations}).")
                yield _emit(AgentExecutionEvent(
                    event_type="TURN_FAILED",
                    payload={"error": "Maximum tool execution iterations exceeded."}
                ))

        finally:
            if hasattr(self.provider, "end_turn"):
                try:
                    self.provider.end_turn(conversation_id=self.store.conversation_id)
                except TypeError:
                    self.provider.end_turn()
                except Exception:
                    pass
            active_turn_context.reset(turn_ctx_token)
            # Mark idle state in summary database
            self.summary_manager.update_idle_state(self.store.conversation_id, not_fully_idle=False)
            if self.task_registry and turn_task:
                if token.is_cancelled:
                    self.task_registry.cancel_task(turn_task.task_id, reason="Turn cancelled.")
                elif turn_task.task_id in self.task_registry._active_tasks:
                    self.task_registry.complete_task(turn_task.task_id)
