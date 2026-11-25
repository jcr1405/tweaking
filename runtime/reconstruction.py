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
    """Conservative local heuristic for token estimation (~3 characters per token for JSON/code)."""
    if not text:
        return 0
    return max(1, len(text) // 3)


def compact_intermediate_tool_output(content_str: str, tool_name: str, step_idx: int) -> str:
    """Compacts an intermediate tool output from an earlier iteration in the active turn
    to prevent quadratic token accumulation while preserving essential semantic signals.
    """
    if not isinstance(content_str, str) or len(content_str) <= 250:
        return content_str

    try:
        data = json.loads(content_str)
        if isinstance(data, dict):
            status = data.get("status", "SUCCESS")
            art_id = data.get("artifact_id")
            if art_id:
                return json.dumps({
                    "status": status,
                    "artifact_id": art_id,
                    "summary": f"Full {tool_name} output offloaded to artifact '{art_id}'. Retained in step {step_idx}."
                })
            elif "results" in data and isinstance(data["results"], list):
                count = len(data["results"])
                titles = [r.get("title", "") for r in data["results"][:3] if isinstance(r, dict)]
                titles_str = ", ".join(f"'{t}'" for t in titles if t)
                return json.dumps({
                    "status": status,
                    "summary": f"{tool_name} returned {count} search results (top: {titles_str}). Details retained in step {step_idx}."
                })
            elif "content" in data:
                url = data.get("url", "")
                raw_c = str(data["content"])
                byte_len = len(raw_c)
                snippet = raw_c[:120].replace("\n", " ").strip()
                return json.dumps({
                    "status": status,
                    "url": url,
                    "summary": f"Fetched {byte_len:,} chars from {url}. Head: {snippet}... [Output retained in step {step_idx}]"
                })
    except Exception:
        pass

    return (
        content_str[:120].strip() +
        f"\n... [Intermediate tool output compacted, {len(content_str):,} chars total. Details retained in step {step_idx}]"
    )


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
        max_context_tokens: int = 8192,
        output_headroom: int = 2048,
    ):
        self.store = trajectory_store
        self.summary_manager = summary_manager
        self.max_context_tokens = max_context_tokens
        self.output_headroom = output_headroom

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
        # Reserve explicit output headroom for model generation so answers are never truncated
        remaining_budget = max(512, self.max_context_tokens - sys_tokens - self.output_headroom)

        # 2. Retrieve all steps chronologically
        steps = self.store.list_steps(start_idx=0)
        total_steps = len(steps)

        # Identify user turn boundaries
        user_indices = [i for i, step in enumerate(steps) if step.step_type == StepType.USER_INPUT]
        last_user_idx = user_indices[-1] if user_indices else -1
        # Protect the last 2 user turns in full; older turns receive aggressive compaction
        recent_boundary = user_indices[-2] if len(user_indices) >= 2 else (last_user_idx if last_user_idx >= 0 else 0)

        # Backward scan to detect intermediate tool actions in the active turn
        has_subsequent_tool_action = [False] * total_steps
        seen_tool_action = False
        for idx in range(total_steps - 1, -1, -1):
            has_subsequent_tool_action[idx] = seen_tool_action
            st = steps[idx]
            if st.step_type == StepType.TOOL_CALL:
                seen_tool_action = True
            elif st.step_type == StepType.AGENT_TURN and (st.step_payload or {}).get("tool_calls"):
                seen_tool_action = True

        # Build raw message sequence from steps
        raw_messages: List[Tuple[Step, Dict[str, Any], int]] = []
        for i, step in enumerate(steps):
            payload = step.step_payload or {}
            msg_dict: Optional[Dict[str, Any]] = None

            if i < last_user_idx:
                # Historical completed turn: prune internal tool execution chatter
                # Only keep user questions and assistant conclusions
                if step.step_type == StepType.USER_INPUT:
                    u_content = payload.get("content", "")
                    if i < recent_boundary and isinstance(u_content, str) and len(u_content) > 600:
                        u_content = u_content[:400] + f"\n... [Historical user message compacted, total {len(u_content):,} chars]"
                    msg_dict = {
                        "role": "user",
                        "content": u_content
                    }
                elif step.step_type == StepType.AGENT_TURN:
                    content = payload.get("content", "")
                    # Compact verbose responses from older turns (> 400 chars)
                    if i < recent_boundary and isinstance(content, str) and len(content) > 400:
                        content = content[:300] + f"\n... [Historical assistant response compacted, total {len(content):,} chars]"
                    if content:
                        msg_dict = {
                            "role": "assistant",
                            "content": content
                        }
                elif step.step_type == StepType.SYSTEM_MSG:
                    msg_dict = {
                        "role": "system",
                        "content": payload.get("content", "")
                    }
            else:
                # Active turn in progress: preserve tool calls and results so model can complete turn
                if step.step_type == StepType.USER_INPUT:
                    msg_dict = {
                        "role": "user",
                        "content": payload.get("content", "")
                    }
                elif step.step_type == StepType.AGENT_TURN:
                    thinking = payload.get("thinking", "")
                    # Strip intermediate reasoning chains from earlier iterations to save tokens
                    if has_subsequent_tool_action[i] and thinking:
                        thinking = ""
                    msg_dict = {
                        "role": "assistant",
                        "content": payload.get("content", ""),
                    }
                    if thinking:
                        msg_dict["thinking"] = thinking
                    if payload.get("tool_calls"):
                        msg_dict["tool_calls"] = payload["tool_calls"]
                elif step.step_type == StepType.TOOL_CALL:
                    msg_dict = {
                        "role": "assistant",
                        "tool_calls": [payload]
                    }
                elif step.step_type == StepType.TOOL_RESULT:
                    tool_content = payload.get("content", "")
                    # Intermediate tool results (followed by subsequent tool calls in active turn)
                    # are aggressively compacted to avoid quadratic prompt blowout.
                    if has_subsequent_tool_action[i]:
                        tool_content = compact_intermediate_tool_output(
                            tool_content, payload.get("name", ""), step.idx
                        )
                    elif isinstance(tool_content, str) and len(tool_content) > 1200:
                        # Latest tool result: bounded to 1200 chars
                        tool_content = tool_content[:700] + f"\n... [Output truncated, total {len(tool_content):,} chars] ...\n" + tool_content[-200:]

                    msg_dict = {
                        "role": "tool",
                        "content": tool_content,
                        "name": payload.get("name", ""),
                        "tool_call_id": payload.get("tool_call_id", "")
                    }
                elif step.step_type == StepType.SYSTEM_MSG:
                    msg_dict = {
                        "role": "system",
                        "content": payload.get("content", "")
                    }

            if msg_dict is not None:
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
                # If this message is a tool result that exceeds remaining budget, truncate it to fit
                if msg_dict.get("role") == "tool" and (sliding_budget - accumulated_tokens) > 100:
                    avail_tokens = sliding_budget - accumulated_tokens
                    avail_chars = max(150, avail_tokens * 3)
                    orig_content = str(msg_dict.get("content", ""))
                    trunc_dict = dict(msg_dict)
                    trunc_dict["content"] = orig_content[:avail_chars] + "\n... [Tool output truncated for context window budget]"
                    t_tokens = estimate_token_count(json.dumps(trunc_dict))
                    selected_messages.append(trunc_dict)
                    accumulated_tokens += t_tokens
                    replayed_step_count += 1
                break

        # Priority Lock: Guarantee the active user prompt is never dropped!
        has_user_msg = any(m.get("role") == "user" for m in selected_messages)
        if not has_user_msg:
            for step, msg_dict, _ in reversed(raw_messages):
                if msg_dict.get("role") == "user":
                    selected_messages.append(msg_dict)
                    replayed_step_count += 1
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
