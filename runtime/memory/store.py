"""Behavioral Memory Store
SQLite-backed persistent store for learned desktop routines, personal preferences,
and episodic interaction records.
Strict Privacy Safeguards:
- Redacts passwords, access tokens, private credentials, and sensitive form inputs.
- Requires explicit user approval for newly discovered candidate routines before auto-execution.
- Stores only semantic action sequences needed to reproduce verified workflows.
"""

import os
import sqlite3
import json
import logging
import threading
from typing import Dict, Any, List, Optional
from pathlib import Path

from runtime.memory.models import (
    UserPreference,
    LearnedRoutine,
    SemanticAction,
    ActionSemanticType,
    EpisodicRecord,
)

logger = logging.getLogger("presence.memory.store")

# Redaction patterns for learning safety
SENSITIVE_PATTERNS = [
    "password", "passwd", "pwd", "secret", "token", "bearer",
    "apikey", "api_key", "cvv", "creditcard", "ssn", "pin", "auth"
]


def sanitize_value_for_memory(val: Any) -> Any:
    """Recursively redacts suspected credentials and authentication tokens."""
    if isinstance(val, str):
        lower = val.lower()
        if any(p in lower for p in SENSITIVE_PATTERNS):
            return "[REDACTED_SENSITIVE_INPUT]"
        return val
    elif isinstance(val, dict):
        clean = {}
        for k, v in val.items():
            if any(p in k.lower() for p in SENSITIVE_PATTERNS):
                clean[k] = "[REDACTED_SENSITIVE_INPUT]"
            else:
                clean[k] = sanitize_value_for_memory(v)
        return clean
    elif isinstance(val, list):
        return [sanitize_value_for_memory(item) for item in val]
    return val


class BehavioralMemoryStore:
    """Manages persistent personal memories, user habits, and learned interaction routines."""

    def __init__(self, db_path: Optional[str] = None):
        self._lock = threading.Lock()
        if db_path:
            self._db_path = Path(db_path)
        else:
            base_dir = Path("data")
            base_dir.mkdir(parents=True, exist_ok=True)
            self._db_path = base_dir / "behavioral_memory.db"

        self._init_db()
        self._seed_default_routines()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Initializes tables for preferences, routines, and episodic records."""
        with self._lock, self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_preferences (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    category TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    metadata_json TEXT NOT NULL
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS learned_routines (
                    routine_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    trigger_pattern TEXT NOT NULL,
                    target_app TEXT NOT NULL,
                    actions_json TEXT NOT NULL,
                    expected_final_state TEXT NOT NULL,
                    initial_state_json TEXT NOT NULL,
                    is_approved INTEGER NOT NULL,
                    confidence REAL NOT NULL,
                    execution_count INTEGER NOT NULL,
                    success_count INTEGER NOT NULL,
                    last_verified_at REAL,
                    created_at REAL NOT NULL,
                    description TEXT NOT NULL
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS episodic_records (
                    record_id TEXT PRIMARY KEY,
                    objective TEXT NOT NULL,
                    routine_id TEXT,
                    outcome TEXT NOT NULL,
                    duration_seconds REAL NOT NULL,
                    steps_executed INTEGER NOT NULL,
                    adaptations_made INTEGER NOT NULL,
                    evidence_summary TEXT NOT NULL,
                    timestamp REAL NOT NULL
                );
            """)
            conn.commit()

    def _seed_default_routines(self) -> None:
        """Seeds canonical personal routines (e.g. YouTube Chrome routine)."""
        # 1. Canonical YouTube Routine
        youtube_routine_id = "routine_youtube_chrome_default"
        if not self.get_routine(youtube_routine_id):
            actions = [
                SemanticAction(
                    action_type=ActionSemanticType.PRESS_SHORTCUT,
                    target="win+r",
                    parameters={"keys": ["win", "r"]},
                    expected_state="Run dialog visible",
                    description="Open Windows Run dialog (Win+R)",
                ),
                SemanticAction(
                    action_type=ActionSemanticType.TYPE_TEXT,
                    target="run_input",
                    parameters={"text": 'chrome --profile-directory="Default" https://www.youtube.com', "press_enter": True},
                    expected_state="Chrome launched with Default profile",
                    description="Launch Chrome with Default profile navigating directly to YouTube",
                ),
                SemanticAction(
                    action_type=ActionSemanticType.WAIT_CONDITION,
                    target="window_title",
                    parameters={"title_contains": "YouTube", "timeout_seconds": 10.0},
                    expected_state="YouTube - Google Chrome",
                    description="Verify YouTube is active and loaded in Google Chrome",
                ),
            ]
            routine = LearnedRoutine(
                routine_id=youtube_routine_id,
                name="Launch YouTube via Chrome Default Profile",
                trigger_pattern=r"\b(?:open|launch|go to|show me|watch)\s+youtube\b",
                target_app="chrome.exe",
                actions=actions,
                expected_final_state="YouTube - Google Chrome",
                initial_state_requirements={"target_browser": "chrome"},
                is_approved=True,
                confidence=0.98,
                description="User's preferred method to open YouTube in Chrome under the Default profile.",
            )
            self.save_routine(routine)

            # Seed user preference for Chrome Default profile
            self.set_preference("browser.preferred_browser", "chrome.exe", category="browser")
            self.set_preference("browser.chrome_profile", "Default", category="browser")

    # -------------------------------------------------------------------------
    # Preferences API
    # -------------------------------------------------------------------------
    def set_preference(self, key: str, value: Any, category: str = "general", confidence: float = 1.0) -> None:
        """Stores or updates a user preference with sanitized values."""
        clean_val = sanitize_value_for_memory(value)
        val_json = json.dumps(clean_val)
        meta_json = json.dumps({})
        now = sqlite3.connect(":memory:").execute("SELECT strftime('%s', 'now')").fetchone()[0]

        with self._lock, self._get_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO user_preferences (key, value_json, category, confidence, updated_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?);
            """, (key, val_json, category, confidence, float(now), meta_json))
            conn.commit()

    def get_preference(self, key: str, default: Any = None) -> Any:
        """Retrieves a user preference value by key."""
        with self._lock, self._get_connection() as conn:
            row = conn.execute("SELECT value_json FROM user_preferences WHERE key = ?;", (key,)).fetchone()
            if row:
                return json.loads(row["value_json"])
        return default

    # -------------------------------------------------------------------------
    # Learned Routines API
    # -------------------------------------------------------------------------
    def save_routine(self, routine: LearnedRoutine) -> None:
        """Saves or updates a learned procedural routine."""
        actions_data = [
            {
                "action_type": a.action_type.value if hasattr(a.action_type, "value") else str(a.action_type),
                "target": a.target,
                "parameters": sanitize_value_for_memory(a.parameters),
                "expected_state": a.expected_state,
                "description": a.description,
            }
            for a in routine.actions
        ]
        actions_json = json.dumps(actions_data)
        init_state_json = json.dumps(sanitize_value_for_memory(routine.initial_state_requirements))

        with self._lock, self._get_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO learned_routines (
                    routine_id, name, trigger_pattern, target_app, actions_json,
                    expected_final_state, initial_state_json, is_approved, confidence,
                    execution_count, success_count, last_verified_at, created_at, description
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                routine.routine_id,
                routine.name,
                routine.trigger_pattern,
                routine.target_app,
                actions_json,
                routine.expected_final_state,
                init_state_json,
                1 if routine.is_approved else 0,
                routine.confidence,
                routine.execution_count,
                routine.success_count,
                routine.last_verified_at,
                routine.created_at,
                routine.description,
            ))
            conn.commit()

    def get_routine(self, routine_id: str) -> Optional[LearnedRoutine]:
        """Loads a LearnedRoutine instance by routine_id."""
        with self._lock, self._get_connection() as conn:
            row = conn.execute("SELECT * FROM learned_routines WHERE routine_id = ?;", (routine_id,)).fetchone()
            if not row:
                return None
            return self._row_to_routine(row)

    def get_routine_by_name(self, name: str) -> Optional[LearnedRoutine]:
        """Loads a LearnedRoutine instance by exact name."""
        with self._lock, self._get_connection() as conn:
            row = conn.execute("SELECT * FROM learned_routines WHERE name = ?;", (name,)).fetchone()
            if not row:
                return None
            return self._row_to_routine(row)

    def find_matching_routines(self, prompt: str, approved_only: bool = True) -> List[LearnedRoutine]:
        """Finds all learned routines matching the provided user prompt."""
        routines = self.list_routines(approved_only=approved_only)
        return [r for r in routines if r.matches(prompt)]

    def list_routines(self, approved_only: bool = False) -> List[LearnedRoutine]:
        """Lists all routines, optionally filtering for approved ones."""
        with self._lock, self._get_connection() as conn:
            if approved_only:
                rows = conn.execute("SELECT * FROM learned_routines WHERE is_approved = 1 ORDER BY confidence DESC;").fetchall()
            else:
                rows = conn.execute("SELECT * FROM learned_routines ORDER BY confidence DESC;").fetchall()
            return [self._row_to_routine(r) for r in rows]

    def approve_routine(self, routine_id: str) -> bool:
        """Approves a candidate routine for autonomous use."""
        with self._lock, self._get_connection() as conn:
            res = conn.execute("UPDATE learned_routines SET is_approved = 1 WHERE routine_id = ?;", (routine_id,))
            conn.commit()
            return res.rowcount > 0

    def delete_routine(self, routine_id: str) -> bool:
        """Deletes a routine from memory."""
        with self._lock, self._get_connection() as conn:
            res = conn.execute("DELETE FROM learned_routines WHERE routine_id = ?;", (routine_id,))
            conn.commit()
            return res.rowcount > 0

    def record_episode(self, record: EpisodicRecord) -> None:
        """Records an execution outcome and updates routine statistics."""
        with self._lock, self._get_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO episodic_records (
                    record_id, objective, routine_id, outcome, duration_seconds,
                    steps_executed, adaptations_made, evidence_summary, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                record.record_id,
                record.objective,
                record.routine_id,
                record.outcome,
                record.duration_seconds,
                record.steps_executed,
                record.adaptations_made,
                record.evidence_summary,
                record.timestamp,
            ))
            if record.routine_id:
                if record.outcome == "SUCCESS":
                    conn.execute("""
                        UPDATE learned_routines
                        SET execution_count = execution_count + 1,
                            success_count = success_count + 1,
                            last_verified_at = ?
                        WHERE routine_id = ?;
                    """, (record.timestamp, record.routine_id))
                else:
                    conn.execute("""
                        UPDATE learned_routines
                        SET execution_count = execution_count + 1
                        WHERE routine_id = ?;
                    """, (record.routine_id,))
            conn.commit()

    def _row_to_routine(self, row: sqlite3.Row) -> LearnedRoutine:
        actions_raw = json.loads(row["actions_json"])
        actions = [
            SemanticAction(
                action_type=ActionSemanticType(a["action_type"]),
                target=a["target"],
                parameters=a.get("parameters", {}),
                expected_state=a.get("expected_state"),
                description=a.get("description", ""),
            )
            for a in actions_raw
        ]
        return LearnedRoutine(
            routine_id=row["routine_id"],
            name=row["name"],
            trigger_pattern=row["trigger_pattern"],
            target_app=row["target_app"],
            actions=actions,
            expected_final_state=row["expected_final_state"],
            initial_state_requirements=json.loads(row["initial_state_json"]),
            is_approved=bool(row["is_approved"]),
            confidence=float(row["confidence"]),
            execution_count=int(row["execution_count"]),
            success_count=int(row["success_count"]),
            last_verified_at=row["last_verified_at"],
            created_at=float(row["created_at"]),
            description=row["description"],
        )
