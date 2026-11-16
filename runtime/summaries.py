"""Global Conversation Summaries & SQLite FTS5 Search Engine
Manages global conversation metadata and sub-millisecond full-text search indexing
across all historical conversations and steps.
"""

import os
import sqlite3
import datetime
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List

logger = logging.getLogger("presence.summaries")


def get_default_summaries_db_path() -> Path:
    """Returns the default path for the global conversation summaries database."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        data_dir = Path(local_app_data) / "Presence" / "data"
    else:
        data_dir = Path("./data").resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "conversation_summaries.db"


class GlobalSummaryManager:
    """Manages global conversation metadata and SQLite FTS5 full-text indexing."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or get_default_summaries_db_path()
        self._conn: Optional[sqlite3.Connection] = None
        self._init_database()

    def _init_database(self) -> None:
        """Initializes connection and provisions relational summaries and FTS5 index."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path),
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False
        )

        with self._conn:
            self._conn.execute("PRAGMA journal_mode = WAL;")
            self._conn.execute("PRAGMA synchronous = NORMAL;")
            self._conn.execute("PRAGMA busy_timeout = 5000;")

            # 1. Global Conversation Summaries Table
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS conversation_summaries (
                    conversation_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT "Untitled Conversation",
                    preview TEXT NOT NULL DEFAULT "",
                    step_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    last_modified_at TEXT NOT NULL,
                    workspace_path TEXT NOT NULL DEFAULT "",
                    status TEXT NOT NULL DEFAULT "IDLE",
                    parent_conversation_id TEXT NOT NULL DEFAULT "",
                    nesting_depth INTEGER NOT NULL DEFAULT 0,
                    not_fully_idle INTEGER NOT NULL DEFAULT 0,
                    killed INTEGER NOT NULL DEFAULT 0,
                    raw_summary TEXT NOT NULL DEFAULT ""
                );
            """)

            # 2. SQLite FTS5 Full-Text Search Virtual Table
            self._conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS conversations_fts USING fts5(
                    conversation_id UNINDEXED,
                    step_idx UNINDEXED,
                    created_at UNINDEXED,
                    title,
                    workspace_path,
                    user_body,
                    agent_body,
                    tool_content,
                    tokenize = 'unicode61 remove_diacritics 2',
                    prefix = '1 2 3'
                );
            """)

    def upsert_summary(
        self,
        conversation_id: str,
        title: str,
        preview: str = "",
        step_count: int = 0,
        workspace_path: str = "",
        status: str = "IDLE",
        parent_conversation_id: str = "",
        nesting_depth: int = 0,
        not_fully_idle: bool = False,
        killed: bool = False,
        raw_summary: str = ""
    ) -> None:
        """Upserts a conversation summary record."""
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._conn:
            self._conn.execute("""
                INSERT INTO conversation_summaries (
                    conversation_id, title, preview, step_count, created_at,
                    last_modified_at, workspace_path, status, parent_conversation_id,
                    nesting_depth, not_fully_idle, killed, raw_summary
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    title = excluded.title,
                    preview = CASE WHEN excluded.preview != '' THEN excluded.preview ELSE conversation_summaries.preview END,
                    step_count = excluded.step_count,
                    last_modified_at = excluded.last_modified_at,
                    workspace_path = excluded.workspace_path,
                    status = excluded.status,
                    not_fully_idle = excluded.not_fully_idle,
                    killed = excluded.killed,
                    raw_summary = CASE WHEN excluded.raw_summary != '' THEN excluded.raw_summary ELSE conversation_summaries.raw_summary END;
            """, (
                conversation_id, title, preview, step_count, now_iso,
                now_iso, workspace_path, status, parent_conversation_id,
                nesting_depth, 1 if not_fully_idle else 0, 1 if killed else 0, raw_summary
            ))

    def update_idle_state(self, conversation_id: str, not_fully_idle: bool) -> None:
        """Updates the not_fully_idle running indicator."""
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._conn:
            self._conn.execute("""
                UPDATE conversation_summaries
                SET not_fully_idle = ?, last_modified_at = ?
                WHERE conversation_id = ?;
            """, (1 if not_fully_idle else 0, now_iso, conversation_id))

    def update_killed_state(self, conversation_id: str, killed: bool) -> None:
        """Marks a conversation as killed/cancelled."""
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._conn:
            self._conn.execute("""
                UPDATE conversation_summaries
                SET killed = ?, not_fully_idle = 0, status = 'KILLED', last_modified_at = ?
                WHERE conversation_id = ?;
            """, (1 if killed else 0, now_iso, conversation_id))

    def update_raw_summary(self, conversation_id: str, raw_summary: str) -> None:
        """Updates the compressed raw_summary for context sliding."""
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._conn:
            self._conn.execute("""
                UPDATE conversation_summaries
                SET raw_summary = ?, last_modified_at = ?
                WHERE conversation_id = ?;
            """, (raw_summary, now_iso, conversation_id))

    def get_summary(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves summary details for a given conversation."""
        cursor = self._conn.execute("""
            SELECT conversation_id, title, preview, step_count, created_at,
                   last_modified_at, workspace_path, status, parent_conversation_id,
                   nesting_depth, not_fully_idle, killed, raw_summary
            FROM conversation_summaries
            WHERE conversation_id = ?;
        """, (conversation_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return {
            "conversation_id": row[0],
            "title": row[1],
            "preview": row[2],
            "step_count": row[3],
            "created_at": row[4],
            "last_modified_at": row[5],
            "workspace_path": row[6],
            "status": row[7],
            "parent_conversation_id": row[8],
            "nesting_depth": row[9],
            "not_fully_idle": bool(row[10]),
            "killed": bool(row[11]),
            "raw_summary": row[12]
        }

    def list_subagents(self, parent_conversation_id: str) -> List[Dict[str, Any]]:
        """Lists all child subagents linked to a parent conversation."""
        cursor = self._conn.execute("""
            SELECT conversation_id, title, preview, step_count, created_at,
                   last_modified_at, workspace_path, status, parent_conversation_id,
                   nesting_depth, not_fully_idle, killed, raw_summary
            FROM conversation_summaries
            WHERE parent_conversation_id = ?
            ORDER BY created_at ASC;
        """, (parent_conversation_id,))
        results = []
        for row in cursor.fetchall():
            results.append({
                "conversation_id": row[0],
                "title": row[1],
                "preview": row[2],
                "step_count": row[3],
                "created_at": row[4],
                "last_modified_at": row[5],
                "workspace_path": row[6],
                "status": row[7],
                "parent_conversation_id": row[8],
                "nesting_depth": row[9],
                "not_fully_idle": bool(row[10]),
                "killed": bool(row[11]),
                "raw_summary": row[12]
            })
        return results

    def index_step_text(
        self,
        conversation_id: str,
        step_idx: int,
        title: str,
        workspace_path: str,
        user_body: str = "",
        agent_body: str = "",
        tool_content: str = ""
    ) -> None:
        """Appends step text into the SQLite FTS5 full-text index."""
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._conn:
            self._conn.execute("""
                INSERT INTO conversations_fts (
                    conversation_id, step_idx, created_at, title,
                    workspace_path, user_body, agent_body, tool_content
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                conversation_id,
                step_idx,
                now_iso,
                title,
                workspace_path,
                user_body,
                agent_body,
                tool_content
            ))

    def search_conversations(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Executes a full-text search across all conversations using FTS5."""
        if not query or not query.strip():
            return []

        # Sanitize query terms for FTS5 (escape internal double quotes)
        clean_terms = [t.replace('"', '""') for t in query.strip().split() if t]
        if not clean_terms:
            return []
        # Wrap terms in quotes to allow arbitrary punctuation without syntax error
        fts_query = " ".join(f'"{term}"' for term in clean_terms)

        try:
            cursor = self._conn.execute("""
                SELECT conversation_id, step_idx, created_at, title, workspace_path,
                       snippet(conversations_fts, 5, '<b>', '</b>', '...', 16) as user_snippet,
                       snippet(conversations_fts, 6, '<b>', '</b>', '...', 16) as agent_snippet,
                       snippet(conversations_fts, 7, '<b>', '</b>', '...', 16) as tool_snippet,
                       rank
                FROM conversations_fts
                WHERE conversations_fts MATCH ?
                ORDER BY rank
                LIMIT ?;
            """, (fts_query, limit))

            results = []
            for row in cursor.fetchall():
                results.append({
                    "conversation_id": row[0],
                    "step_idx": int(row[1]),
                    "created_at": row[2],
                    "title": row[3],
                    "workspace_path": row[4],
                    "user_snippet": row[5] or "",
                    "agent_snippet": row[6] or "",
                    "tool_snippet": row[7] or "",
                    "rank": float(row[8])
                })
            return results
        except sqlite3.OperationalError as e:
            logger.warning(f"FTS5 search error for query '{query}': {e}")
            return []

    def close(self) -> None:
        """Closes the underlying database connection cleanly."""
        if self._conn:
            try:
                self._conn.close()
            except Exception as e:
                logger.error(f"Error closing GlobalSummaryManager: {e}")
            finally:
                self._conn = None
