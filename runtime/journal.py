"""Streaming Journal Subsystem (transcript.jsonl)
Manages append-only JSONL streaming event logs.
Provides asynchronous event logging for live UI rendering and reconciliation
with authoritative SQLite steps.
"""

import os
import json
import logging
import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List

from runtime.trajectory import Step, StepType, StepStatus

logger = logging.getLogger("presence.journal")


def get_default_logs_dir() -> Path:
    """Returns the default directory for conversation transcript logs."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        base_dir = Path(local_app_data) / "Presence" / "logs"
    else:
        base_dir = Path("./logs").resolve()
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir


class EventJournal:
    """Manages append-only JSONL event stream for a specific conversation."""

    def __init__(self, conversation_id: str, logs_dir: Optional[Path] = None):
        self.conversation_id = conversation_id
        self.logs_dir = (logs_dir or get_default_logs_dir()) / conversation_id
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.transcript_path = self.logs_dir / "transcript.jsonl"
        self._file_handle = None

    def _ensure_open(self):
        """Ensures the write file handle is open in append mode."""
        if self._file_handle is None or self._file_handle.closed:
            self._file_handle = open(self.transcript_path, "a", encoding="utf-8", buffering=1)

    def append_event(
        self,
        step_index: int,
        source: str,
        event_type: str,
        status: str,
        content: str = "",
        thinking: str = "",
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        truncated_fields: Optional[List[str]] = None,
        extra: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Appends a strongly typed event record to transcript.jsonl."""
        self._ensure_open()
        record = {
            "step_index": step_index,
            "source": source,
            "type": event_type,
            "status": status,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "content": content,
            "thinking": thinking,
            "tool_calls": tool_calls or [],
            "truncated_fields": truncated_fields or []
        }
        if extra:
            record.update(extra)

        line = json.dumps(record, ensure_ascii=False) + "\n"
        self._file_handle.write(line)
        self._file_handle.flush()
        return record

    def append_step_event(self, step: Step) -> Dict[str, Any]:
        """Helper to append an event derived from a committed Step."""
        type_str_map = {
            StepType.USER_INPUT: "USER_INPUT",
            StepType.AGENT_TURN: "PLANNER_RESPONSE",
            StepType.TOOL_CALL: "GENERIC",
            StepType.TOOL_RESULT: "GENERIC",
            StepType.SYSTEM_MSG: "SYSTEM_MESSAGE"
        }
        status_str_map = {
            StepStatus.PENDING: "PENDING",
            StepStatus.DONE: "DONE",
            StepStatus.ERROR: "ERROR",
            StepStatus.INTERRUPTED: "INTERRUPTED"
        }

        source = "USER_EXPLICIT" if step.step_type == StepType.USER_INPUT else "MODEL"
        if step.step_type == StepType.SYSTEM_MSG:
            source = "SYSTEM"

        payload = step.step_payload or {}
        content = payload.get("content", "")
        thinking = payload.get("thinking", "")
        tool_calls = payload.get("tool_calls", [])

        return self.append_event(
            step_index=step.idx,
            source=source,
            event_type=type_str_map.get(step.step_type, "GENERIC"),
            status=status_str_map.get(step.status, "DONE"),
            content=content,
            thinking=thinking,
            tool_calls=tool_calls,
            extra={"has_subtrajectory": step.has_subtrajectory} if step.has_subtrajectory else None
        )

    def read_transcript(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Reads transcript records from JSONL disk stream."""
        if not self.transcript_path.exists():
            return []

        records = []
        with open(self.transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError as e:
                        logger.warning(f"Skipping corrupt JSONL record: {e}")
        if limit is not None:
            return records[-limit:]
        return records

    def reconcile_with_steps(self, steps: List[Step]) -> int:
        """Reconciles transcript.jsonl against authoritative SQLite steps.
        If any step is missing in JSONL, it is backfilled.
        Returns the count of backfilled steps.
        """
        existing_records = self.read_transcript()
        existing_indices = {r["step_index"] for r in existing_records if "step_index" in r}

        backfilled_count = 0
        for step in steps:
            if step.idx not in existing_indices:
                self.append_step_event(step)
                backfilled_count += 1
                logger.info(f"Reconciled missing step {step.idx} into transcript.jsonl")

        return backfilled_count

    def close(self) -> None:
        """Flushes and closes the open file handle."""
        if self._file_handle and not self._file_handle.closed:
            try:
                self._file_handle.flush()
                self._file_handle.close()
            except Exception as e:
                logger.error(f"Error closing EventJournal: {e}")
            finally:
                self._file_handle = None
