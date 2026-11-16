"""Persistence Layer Subsystem
ACID-compliant SQLite engine configured with Write-Ahead Logging (WAL).
Manages configuration, runtime recovery states, and health telemetry.
"""

import os
import time
import sqlite3
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

logger = logging.getLogger("presence.persistence")


def get_default_db_path() -> Path:
    """Returns the default directory and path for the runtime SQLite database."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        data_dir = Path(local_app_data) / "Presence" / "data"
    else:
        data_dir = Path("./data").resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "runtime.db"


class PersistenceManager:
    """Manages transactional SQLite storage with WAL mode enabled."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or get_default_db_path()
        self._conn: Optional[sqlite3.Connection] = None
        self._init_database()

    def _init_database(self) -> None:
        """Initializes connection and applies WAL pragmas and schema definitions."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path),
            timeout=5.0,
            isolation_level=None,  # Autocommit mode; we manage explicit transactions
            check_same_thread=False,
        )

        with self._conn:
            # Enforce WAL mode and strict transactional pragmas
            self._conn.execute("PRAGMA journal_mode = WAL;")
            self._conn.execute("PRAGMA synchronous = NORMAL;")
            self._conn.execute("PRAGMA foreign_keys = ON;")
            self._conn.execute("PRAGMA busy_timeout = 5000;")

            # 1. Runtime Configuration Store
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS runtime_config (
                    config_key TEXT PRIMARY KEY,
                    config_value TEXT NOT NULL,
                    updated_at_utc REAL NOT NULL
                );
            """)

            # 2. Runtime Recovery & Lifecycle Journal
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS runtime_recovery (
                    instance_id TEXT PRIMARY KEY,
                    pid INTEGER NOT NULL,
                    start_time_utc REAL NOT NULL,
                    last_heartbeat_utc REAL NOT NULL,
                    clean_shutdown INTEGER DEFAULT 0,
                    exit_time_utc REAL
                );
            """)

            # 3. System Telemetry Samples
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS system_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp_utc REAL NOT NULL,
                    working_set_bytes INTEGER NOT NULL,
                    private_bytes INTEGER NOT NULL,
                    cpu_percent REAL NOT NULL
                );
            """)

        logger.info(f"Persistence engine initialized in WAL mode at {self.db_path}")

    def set_config(self, key: str, value: str) -> None:
        """Atomically stores or updates a configuration value."""
        now = time.time()
        with self._conn:
            self._conn.execute("""
                INSERT INTO runtime_config (config_key, config_value, updated_at_utc)
                VALUES (?, ?, ?)
                ON CONFLICT(config_key) DO UPDATE SET
                    config_value = excluded.config_value,
                    updated_at_utc = excluded.updated_at_utc;
            """, (key, str(value), now))

    def get_config(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Retrieves a configuration value by key."""
        cursor = self._conn.execute(
            "SELECT config_value FROM runtime_config WHERE config_key = ?;",
            (key,),
        )
        row = cursor.fetchone()
        return row[0] if row else default

    def get_all_config(self) -> Dict[str, str]:
        """Returns all persisted configuration key-values."""
        cursor = self._conn.execute("SELECT config_key, config_value FROM runtime_config;")
        return {row[0]: row[1] for row in cursor.fetchall()}

    def record_start(self, instance_id: str, pid: int) -> None:
        """Records initial startup of an execution instance."""
        now = time.time()
        with self._conn:
            self._conn.execute("""
                INSERT INTO runtime_recovery (instance_id, pid, start_time_utc, last_heartbeat_utc, clean_shutdown)
                VALUES (?, ?, ?, ?, 0);
            """, (instance_id, pid, now, now))

    def record_heartbeat(self, instance_id: str) -> None:
        """Updates the periodic heartbeat timestamp for an instance."""
        now = time.time()
        with self._conn:
            self._conn.execute("""
                UPDATE runtime_recovery
                SET last_heartbeat_utc = ?
                WHERE instance_id = ?;
            """, (now, instance_id))

    def record_clean_shutdown(self, instance_id: str) -> None:
        """Marks an instance as having terminated cleanly."""
        now = time.time()
        with self._conn:
            self._conn.execute("""
                UPDATE runtime_recovery
                SET clean_shutdown = 1, exit_time_utc = ?
                WHERE instance_id = ?;
            """, (now, instance_id))

    def detect_unclean_shutdowns(self, current_instance_id: str) -> List[Dict[str, Any]]:
        """Identifies any previous instances that died without clean shutdown."""
        cursor = self._conn.execute("""
            SELECT instance_id, pid, start_time_utc, last_heartbeat_utc
            FROM runtime_recovery
            WHERE clean_shutdown = 0 AND instance_id != ?;
        """, (current_instance_id,))
        results = []
        for row in cursor.fetchall():
            results.append({
                "instance_id": row[0],
                "pid": row[1],
                "start_time_utc": row[2],
                "last_heartbeat_utc": row[3],
            })
        return results

    def checkpoint_wal(self) -> None:
        """Forces a passive SQLite WAL checkpoint to flush committed frames to the main database file."""
        if self._conn:
            self._conn.execute("PRAGMA wal_checkpoint(PASSIVE);")

    def close(self) -> None:
        """Flushes WAL and closes database connection."""
        if self._conn:
            try:
                self.checkpoint_wal()
                self._conn.close()
            except Exception as e:
                logger.warning(f"Error closing persistence database: {e}")
            finally:
                self._conn = None
