"""Trajectory Storage & Monotonic Step Engine
Manages conversation-scoped SQLite databases with WAL mode.
Provides atomic step insertion, monotonic primary keys, and replay queries.
"""

import os
import json
import sqlite3
import datetime
import logging
from enum import IntEnum
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, Any, List

from runtime.conversation import ConversationMetadata

logger = logging.getLogger("presence.trajectory")


class StepType(IntEnum):
    """Enumeration of execution step types matching forensic observations."""
    USER_INPUT = 0
    AGENT_TURN = 1
    TOOL_CALL = 2
    TOOL_RESULT = 3
    SYSTEM_MSG = 4


class StepStatus(IntEnum):
    """Execution status of an individual step."""
    PENDING = 0
    DONE = 1
    ERROR = 2
    INTERRUPTED = 3


@dataclass
class Step:
    """Strongly-typed execution step record."""
    idx: int
    step_type: StepType
    status: StepStatus = StepStatus.PENDING
    created_at: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())
    step_payload: Dict[str, Any] = field(default_factory=dict)
    has_subtrajectory: bool = False
    subagent_conversation_id: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    error_details: str = ""
    permissions_granted: List[str] = field(default_factory=list)
    task_details: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Converts step to dictionary."""
        d = asdict(self)
        d["step_type"] = int(self.step_type)
        d["status"] = int(self.status)
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Step":
        """Reconstructs step from dictionary."""
        return cls(
            idx=int(data["idx"]),
            step_type=StepType(int(data["step_type"])),
            status=StepStatus(int(data.get("status", 0))),
            created_at=data.get("created_at", datetime.datetime.now(datetime.timezone.utc).isoformat()),
            step_payload=data.get("step_payload", {}),
            has_subtrajectory=bool(data.get("has_subtrajectory", False)),
            subagent_conversation_id=data.get("subagent_conversation_id", ""),
            metadata=data.get("metadata", {}),
            error_details=data.get("error_details", ""),
            permissions_granted=data.get("permissions_granted", []),
            task_details=data.get("task_details", "")
        )


def get_default_conversations_dir() -> Path:
    """Returns the default directory path for scoped conversation databases."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        base_dir = Path(local_app_data) / "Presence" / "conversations"
    else:
        base_dir = Path("./data/conversations").resolve()
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir


class TrajectoryStore:
    """Conversation-scoped relational SQLite store for execution trajectories."""

    def __init__(self, conversation_id: str, db_dir: Optional[Path] = None):
        self.conversation_id = conversation_id
        self.db_dir = db_dir or get_default_conversations_dir()
        self.db_path = self.db_dir / f"{conversation_id}.db"
        self._conn: Optional[sqlite3.Connection] = None
        self._init_database()

    def _init_database(self) -> None:
        """Initializes connection and provisions relational schema with WAL mode."""
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path),
            timeout=5.0,
            isolation_level=None,  # Autocommit mode; explicit BEGIN/COMMIT
            check_same_thread=False
        )

        with self._conn:
            # Enforce WAL and SQLite durability pragmas
            self._conn.execute("PRAGMA journal_mode = WAL;")
            self._conn.execute("PRAGMA synchronous = NORMAL;")
            self._conn.execute("PRAGMA foreign_keys = ON;")
            self._conn.execute("PRAGMA busy_timeout = 5000;")

            # 1. Trajectory Metadata
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS trajectory_meta (
                    conversation_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT "",
                    created_at TEXT NOT NULL,
                    last_modified_at TEXT NOT NULL,
                    parent_conversation_id TEXT NOT NULL DEFAULT "",
                    nesting_depth INTEGER NOT NULL DEFAULT 0,
                    workspace_path TEXT NOT NULL DEFAULT "",
                    workspace_mode TEXT NOT NULL DEFAULT "inherit",
                    status TEXT NOT NULL DEFAULT "ACTIVE"
                );
            """)

            # 2. Monotonic Execution Steps Table (Canonical Truth)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS steps (
                    idx INTEGER PRIMARY KEY,
                    step_type INTEGER NOT NULL,
                    status INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    has_subtrajectory INTEGER NOT NULL DEFAULT 0,
                    subagent_conversation_id TEXT DEFAULT "",
                    metadata TEXT NOT NULL DEFAULT "{}",
                    error_details TEXT DEFAULT "",
                    permissions_granted TEXT NOT NULL DEFAULT "[]",
                    task_details TEXT DEFAULT "",
                    step_payload TEXT NOT NULL
                );
            """)

            # 3. Generation Metadata & Token Accounting
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS gen_metadata (
                    step_idx INTEGER PRIMARY KEY,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    cached_tokens INTEGER NOT NULL DEFAULT 0,
                    inference_duration_ms REAL NOT NULL DEFAULT 0.0,
                    model_name TEXT NOT NULL DEFAULT "",
                    provider_name TEXT NOT NULL DEFAULT "",
                    FOREIGN KEY(step_idx) REFERENCES steps(idx) ON DELETE CASCADE
                );
            """)

            # 4. Step-Level Permissions Audit
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS permissions_audit (
                    permission_id TEXT PRIMARY KEY,
                    step_idx INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    tier TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    justification TEXT NOT NULL DEFAULT "",
                    timestamp TEXT NOT NULL,
                    FOREIGN KEY(step_idx) REFERENCES steps(idx) ON DELETE CASCADE
                );
            """)

    def save_metadata(self, meta: ConversationMetadata) -> None:
        """Saves or updates conversation metadata."""
        with self._conn:
            self._conn.execute("""
                INSERT INTO trajectory_meta (
                    conversation_id, title, created_at, last_modified_at,
                    parent_conversation_id, nesting_depth, workspace_path,
                    workspace_mode, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    title = excluded.title,
                    last_modified_at = excluded.last_modified_at,
                    workspace_path = excluded.workspace_path,
                    workspace_mode = excluded.workspace_mode,
                    status = excluded.status;
            """, (
                meta.conversation_id,
                meta.title,
                meta.created_at,
                meta.last_modified_at,
                meta.parent_conversation_id,
                meta.nesting_depth,
                meta.workspace_path,
                meta.workspace_mode,
                meta.status
            ))

    def get_metadata(self) -> Optional[ConversationMetadata]:
        """Retrieves conversation metadata from store."""
        cursor = self._conn.execute("""
            SELECT conversation_id, title, created_at, last_modified_at,
                   parent_conversation_id, nesting_depth, workspace_path,
                   workspace_mode, status
            FROM trajectory_meta
            WHERE conversation_id = ?;
        """, (self.conversation_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return ConversationMetadata(
            conversation_id=row[0],
            title=row[1],
            created_at=row[2],
            last_modified_at=row[3],
            parent_conversation_id=row[4],
            nesting_depth=row[5],
            workspace_path=row[6],
            workspace_mode=row[7],
            status=row[8]
        )

    def get_next_step_index(self) -> int:
        """Returns the next monotonic step index (0 if empty)."""
        cursor = self._conn.execute("SELECT COALESCE(MAX(idx), -1) + 1 FROM steps;")
        row = cursor.fetchone()
        return row[0] if row else 0

    def append_step(self, step: Step) -> int:
        """Appends a new step atomically. Enforces monotonic ordering."""
        with self._conn:
            self._conn.execute("""
                INSERT INTO steps (
                    idx, step_type, status, created_at, has_subtrajectory,
                    subagent_conversation_id, metadata, error_details,
                    permissions_granted, task_details, step_payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                step.idx,
                int(step.step_type),
                int(step.status),
                step.created_at,
                1 if step.has_subtrajectory else 0,
                step.subagent_conversation_id,
                json.dumps(step.metadata),
                step.error_details,
                json.dumps(step.permissions_granted),
                step.task_details,
                json.dumps(step.step_payload)
            ))
            # Touch last_modified_at in metadata
            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
            self._conn.execute(
                "UPDATE trajectory_meta SET last_modified_at = ? WHERE conversation_id = ?;",
                (now_iso, self.conversation_id)
            )
        return step.idx

    def update_step_status(self, idx: int, status: StepStatus, error_details: str = "") -> bool:
        """Updates status and error details for an existing step."""
        with self._conn:
            cursor = self._conn.execute("""
                UPDATE steps
                SET status = ?, error_details = ?
                WHERE idx = ?;
            """, (int(status), error_details, idx))
            return cursor.rowcount > 0

    def get_step(self, idx: int) -> Optional[Step]:
        """Retrieves a specific step by its index."""
        cursor = self._conn.execute("""
            SELECT idx, step_type, status, created_at, has_subtrajectory,
                   subagent_conversation_id, metadata, error_details,
                   permissions_granted, task_details, step_payload
            FROM steps
            WHERE idx = ?;
        """, (idx,))
        row = cursor.fetchone()
        if not row:
            return None
        return Step(
            idx=row[0],
            step_type=StepType(row[1]),
            status=StepStatus(row[2]),
            created_at=row[3],
            has_subtrajectory=bool(row[4]),
            subagent_conversation_id=row[5] or "",
            metadata=json.loads(row[6]) if row[6] else {},
            error_details=row[7] or "",
            permissions_granted=json.loads(row[8]) if row[8] else [],
            task_details=row[9] or "",
            step_payload=json.loads(row[10]) if row[10] else {}
        )

    def list_steps(self, start_idx: int = 0, limit: Optional[int] = None) -> List[Step]:
        """Replays steps in strict chronological order."""
        query = """
            SELECT idx, step_type, status, created_at, has_subtrajectory,
                   subagent_conversation_id, metadata, error_details,
                   permissions_granted, task_details, step_payload
            FROM steps
            WHERE idx >= ?
            ORDER BY idx ASC
        """
        params: List[Any] = [start_idx]
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)

        cursor = self._conn.execute(query, tuple(params))
        steps = []
        for row in cursor.fetchall():
            steps.append(Step(
                idx=row[0],
                step_type=StepType(row[1]),
                status=StepStatus(row[2]),
                created_at=row[3],
                has_subtrajectory=bool(row[4]),
                subagent_conversation_id=row[5] or "",
                metadata=json.loads(row[6]) if row[6] else {},
                error_details=row[7] or "",
                permissions_granted=json.loads(row[8]) if row[8] else [],
                task_details=row[9] or "",
                step_payload=json.loads(row[10]) if row[10] else {}
            ))
        return steps

    def get_step_count(self) -> int:
        """Returns the total number of committed steps in this trajectory."""
        cursor = self._conn.execute("SELECT COUNT(*) FROM steps;")
        row = cursor.fetchone()
        return row[0] if row else 0

    def record_generation_metadata(
        self,
        step_idx: int,
        prompt_tokens: int,
        completion_tokens: int,
        cached_tokens: int,
        duration_ms: float,
        model_name: str,
        provider_name: str
    ) -> None:
        """Records token accounting and inference telemetry for a step."""
        with self._conn:
            self._conn.execute("""
                INSERT OR REPLACE INTO gen_metadata (
                    step_idx, prompt_tokens, completion_tokens, cached_tokens,
                    inference_duration_ms, model_name, provider_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?);
            """, (
                step_idx, prompt_tokens, completion_tokens, cached_tokens,
                duration_ms, model_name, provider_name
            ))

    def record_permission_audit(
        self,
        permission_id: str,
        step_idx: int,
        action: str,
        tier: str,
        decision: str,
        justification: str = ""
    ) -> None:
        """Records an authoritative permission check decision for a step."""
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._conn:
            self._conn.execute("""
                INSERT INTO permissions_audit (
                    permission_id, step_idx, action, tier, decision,
                    justification, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?);
            """, (
                permission_id, step_idx, action, tier, decision,
                justification, now_iso
            ))

    def close(self) -> None:
        """Closes the underlying database connection cleanly."""
        if self._conn:
            try:
                self._conn.close()
            except Exception as e:
                logger.error(f"Error closing TrajectoryStore connection: {e}")
            finally:
                self._conn = None
