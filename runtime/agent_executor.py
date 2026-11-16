"""Single-Agent Execution Engine
Coordinates the multi-step agent turn loop: context reconstruction, model requests,
streaming, manual tool authorization and execution, and dual-journal persistence.
"""

import json
import logging
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
        artifact_store: Optional[ArtifactStore] = None
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
        max_tool_iterations: int = 10
    ) -> Iterator[AgentExecutionEvent]:
        """Executes a full turn, yielding normalized streaming events."""
        token = cancellation_token or CancellationToken()

        if token.is_cancelled:
            yield AgentExecutionEvent(
                event_type="TURN_CANCELLED",
                payload={"reason": "Turn cancelled before execution."}
            )
            return

        # 1. Commit and record USER_INPUT step
        user_idx = self.store.get_next_step_index()
        user_step = Step(
            idx=user_idx,
            step_type=StepType.USER_INPUT,
            status=StepStatus.DONE,
            step_payload={"content": user_input}
        )
        self.store.append_step(user_step)
        self.journal.append_step_event(user_step)

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

        yield AgentExecutionEvent(
            event_type="TURN_STARTED",
            step_index=user_idx,
            payload={"user_input": user_input}
        )

        # Update running state
        self.summary_manager.update_idle_state(self.store.conversation_id, not_fully_idle=True)

        iterations = 0
        try:
            while iterations < max_tool_iterations:
                iterations += 1

                if token.is_cancelled:
                    logger.info("Agent execution cancelled between iterations.")
                    yield AgentExecutionEvent(
                        event_type="TURN_CANCELLED",
                        payload={"reason": "Cancelled by user token."}
                    )
                    break

                # 2. Reconstruct context deterministically from SQLite
                reconstructed = self.assembler.assemble(
                    system_instructions=system_prompt,
                    desktop_ambient_context=desktop_ambient_context
                )

                # 3. Retrieve available tool schemas
                tool_declarations = self.tool_registry.get_declarations()

                yield AgentExecutionEvent(
                    event_type="MODEL_REQUESTED",
                    payload={
                        "iteration": iterations,
                        "model": self.provider.model_id,
                        "reconstructed_steps": reconstructed.replayed_steps
                    }
                )

                # 4. Stream model response
                accumulated_text = ""
                extracted_tool_calls: List[Dict[str, Any]] = []
                last_usage: Optional[Dict[str, int]] = None

                try:
                    stream_iter = self.provider.generate_stream(
                        system_prompt=reconstructed.system_prompt,
                        messages=reconstructed.messages,
                        tools=tool_declarations if tool_declarations else None,
                        cancellation_token=token
                    )

                    for chunk in stream_iter:
                        if token.is_cancelled:
                            yield AgentExecutionEvent(
                                event_type="TURN_CANCELLED",
                                payload={"reason": "Cancelled during model streaming."}
                            )
                            return

                        if chunk.text_delta:
                            accumulated_text += chunk.text_delta
                            yield AgentExecutionEvent(
                                event_type="TEXT_DELTA",
                                payload={"delta": chunk.text_delta}
                            )

                        if chunk.tool_calls:
                            extracted_tool_calls.extend(chunk.tool_calls)

                        if chunk.usage:
                            last_usage = chunk.usage
                            yield AgentExecutionEvent(
                                event_type="USAGE_UPDATED",
                                payload=chunk.usage
                            )

                except Exception as model_err:
                    err_msg = str(model_err)
                    logger.error(f"Model generation failure: {err_msg}")
                    yield AgentExecutionEvent(
                        event_type="TURN_FAILED",
                        payload={"error": err_msg, "iteration": iterations}
                    )
                    return

                # 5. Handle model tool requests vs terminal text response
                if not extracted_tool_calls:
                    # Model completed turn with text response!
                    agent_idx = self.store.get_next_step_index()
                    agent_step = Step(
                        idx=agent_idx,
                        step_type=StepType.AGENT_TURN,
                        status=StepStatus.DONE,
                        step_payload={"content": accumulated_text}
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
                        agent_body=accumulated_text
                    )

                    yield AgentExecutionEvent(
                        event_type="TURN_COMPLETED",
                        step_index=agent_idx,
                        payload={
                            "final_text": accumulated_text,
                            "iterations": iterations,
                            "usage": last_usage
                        }
                    )
                    break

                # Model requested tool call(s)
                for tc in extracted_tool_calls:
                    if token.is_cancelled:
                        yield AgentExecutionEvent(
                            event_type="TURN_CANCELLED",
                            payload={"reason": "Cancelled before tool execution."}
                        )
                        return

                    call_id = tc.get("call_id", f"call_{iterations}")
                    tool_name = tc.get("name", "")
                    tool_args = tc.get("args", {})

                    yield AgentExecutionEvent(
                        event_type="TOOL_REQUESTED",
                        payload={"call_id": call_id, "name": tool_name, "args": tool_args}
                    )

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

                    # Execute tool through Permission Boundary
                    yield AgentExecutionEvent(
                        event_type="TOOL_STARTED",
                        step_index=tc_idx,
                        payload={"name": tool_name}
                    )

                    exec_res = self.tool_registry.execute(
                        tool_id=tool_name,
                        args=tool_args,
                        permission_boundary=self.permission_boundary
                    )

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

                    tr_step = Step(
                        idx=tr_idx,
                        step_type=StepType.TOOL_RESULT,
                        status=StepStatus.DONE if exec_res.get("status") == "SUCCESS" else StepStatus.ERROR,
                        step_payload=tr_payload
                    )
                    self.store.append_step(tr_step)
                    self.journal.append_step_event(tr_step)

                    # Index tool output into FTS5 for search recall (index preview so large blobs don't bloat FTS)
                    self.summary_manager.index_step_text(
                        conversation_id=self.store.conversation_id,
                        step_idx=tr_idx,
                        title=title,
                        workspace_path=workspace,
                        tool_content=f"{tool_name}: {preview_content[:2000]}"
                    )

                    if exec_res.get("status") == "SUCCESS":
                        yield AgentExecutionEvent(
                            event_type="TOOL_COMPLETED",
                            step_index=tr_idx,
                            payload={
                                "name": tool_name,
                                "result": preview_content if offloaded else exec_res.get("result"),
                                "artifact_id": artifact_id,
                                "offloaded": offloaded
                            }
                        )
                    else:
                        yield AgentExecutionEvent(
                            event_type="TOOL_FAILED",
                            step_index=tr_idx,
                            payload={"name": tool_name, "error": exec_res.get("error")}
                        )

            if iterations >= max_tool_iterations:
                logger.warning(f"Turn reached max_tool_iterations ({max_tool_iterations}).")
                yield AgentExecutionEvent(
                    event_type="TURN_FAILED",
                    payload={"error": "Maximum tool execution iterations exceeded."}
                )

        finally:
            # Mark idle state in summary database
            self.summary_manager.update_idle_state(self.store.conversation_id, not_fully_idle=False)
