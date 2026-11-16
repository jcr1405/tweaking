"""Context Reconstruction Engine
Deterministic local context assembly from SQLite steps and conversation summaries.
Provides sliding-window token management and provider-independent history replay.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List, Tuple

from runtime.trajectory import TrajectoryStore, Step, StepType, StepStatus
from runtime.summaries import GlobalSummaryManager

logger = logging.getLogger("presence.reconstruction")


def estimate_token_count(text: str) -> int:
    """Fast local heuristic for token estimation (~4 characters per token)."""
    if not text:
        return 0
    return max(1, len(text) // 4)


@dataclass
class ReconstructedContext:
    """Assembled prompt context ready for dispatch to any model provider."""
    conversation_id: str
    system_prompt: str
    messages: List[Dict[str, Any]] = field(default_factory=list)
    estimated_tokens: int = 0
    is_compacted: bool = False
    total_steps: int = 0
    replayed_steps: int = 0
    workspace_path: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Converts context to dictionary representation."""
        return {
            "conversation_id": self.conversation_id,
            "system_prompt": self.system_prompt,
            "messages": self.messages,
            "estimated_tokens": self.estimated_tokens,
            "is_compacted": self.is_compacted,
            "total_steps": self.total_steps,
            "replayed_steps": self.replayed_steps,
            "workspace_path": self.workspace_path
        }


class ContextAssembler:
    """Assembles prompt context from authoritative SQLite steps."""

    def __init__(
        self,
        trajectory_store: TrajectoryStore,
        summary_manager: Optional[GlobalSummaryManager] = None,
        max_context_tokens: int = 8192
    ):
        self.store = trajectory_store
        self.summary_manager = summary_manager
        self.max_context_tokens = max_context_tokens

    def assemble(
        self,
        system_instructions: str = "You are a persistent desktop companion.",
        desktop_ambient_context: Optional[Dict[str, Any]] = None
    ) -> ReconstructedContext:
        """Deterministically reconstructs prompt context from step records."""
        metadata = self.store.get_metadata()
        workspace_path = metadata.workspace_path if metadata else ""

        # 1. Base System Prompt
        sys_prompt = system_instructions
        if desktop_ambient_context:
            sys_prompt += f"\n\n[Active Desktop Environment]:\n{json.dumps(desktop_ambient_context, indent=2)}"

        sys_tokens = estimate_token_count(sys_prompt)
        remaining_budget = self.max_context_tokens - sys_tokens

        # 2. Retrieve all steps chronologically
        steps = self.store.list_steps(start_idx=0)
        total_steps = len(steps)

        # Build raw message sequence from steps
        raw_messages: List[Tuple[Step, Dict[str, Any], int]] = []
        for step in steps:
            payload = step.step_payload or {}
            msg_dict: Dict[str, Any] = {}

            if step.step_type == StepType.USER_INPUT:
                msg_dict = {
                    "role": "user",
                    "content": payload.get("content", "")
                }
            elif step.step_type == StepType.AGENT_TURN:
                msg_dict = {
                    "role": "assistant",
                    "content": payload.get("content", ""),
                    "thinking": payload.get("thinking", "")
                }
                if payload.get("tool_calls"):
                    msg_dict["tool_calls"] = payload["tool_calls"]
            elif step.step_type == StepType.TOOL_CALL:
                msg_dict = {
                    "role": "assistant",
                    "tool_calls": [payload]
                }
            elif step.step_type == StepType.TOOL_RESULT:
                msg_dict = {
                    "role": "tool",
                    "content": payload.get("content", ""),
                    "name": payload.get("name", ""),
                    "tool_call_id": payload.get("tool_call_id", "")
                }
            elif step.step_type == StepType.SYSTEM_MSG:
                msg_dict = {
                    "role": "system",
                    "content": payload.get("content", "")
                }

            msg_tokens = estimate_token_count(json.dumps(msg_dict))
            raw_messages.append((step, msg_dict, msg_tokens))

        total_message_tokens = sum(m[2] for m in raw_messages)

        # 3. Check if full history fits into budget
        if total_message_tokens <= remaining_budget:
            # Full history fits
            messages = [m[1] for m in raw_messages]
            return ReconstructedContext(
                conversation_id=self.store.conversation_id,
                system_prompt=sys_prompt,
                messages=messages,
                estimated_tokens=sys_tokens + total_message_tokens,
                is_compacted=False,
                total_steps=total_steps,
                replayed_steps=total_steps,
                workspace_path=workspace_path
            )

        # 4. History exceeds budget -> apply sliding window + compact summary
        is_compacted = True
        raw_summary_text = ""
        if self.summary_manager:
            sum_rec = self.summary_manager.get_summary(self.store.conversation_id)
            if sum_rec and sum_rec.get("raw_summary"):
                raw_summary_text = sum_rec["raw_summary"]

        summary_msg = None
        summary_tokens = 0
        if raw_summary_text:
            summary_msg = {
                "role": "system",
                "content": f"[Historical Conversation Summary]:\n{raw_summary_text}"
            }
            summary_tokens = estimate_token_count(summary_msg["content"])

        sliding_budget = remaining_budget - summary_tokens

        # Accumulate recent messages backwards from newest to oldest
        selected_messages: List[Dict[str, Any]] = []
        accumulated_tokens = 0
        replayed_step_count = 0

        for step, msg_dict, msg_tokens in reversed(raw_messages):
            if accumulated_tokens + msg_tokens <= sliding_budget:
                selected_messages.append(msg_dict)
                accumulated_tokens += msg_tokens
                replayed_step_count += 1
            else:
                break

        # Reverse back to chronological order
        selected_messages.reverse()

        final_messages: List[Dict[str, Any]] = []
        if summary_msg:
            final_messages.append(summary_msg)
        final_messages.extend(selected_messages)

        return ReconstructedContext(
            conversation_id=self.store.conversation_id,
            system_prompt=sys_prompt,
            messages=final_messages,
            estimated_tokens=sys_tokens + summary_tokens + accumulated_tokens,
            is_compacted=is_compacted,
            total_steps=total_steps,
            replayed_steps=replayed_step_count,
            workspace_path=workspace_path
        )
