"""Persistence Layer Subsystem
ACID-compliant SQLite engine configured with Write-Ahead Logging (WAL).
Manages configuration, runtime recovery states, and health telemetry.
"""

import os
import time
import json
import sqlite3
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple, Union

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

    def __init__(self, db_path: Optional[Union[Path, str]] = None):
        self.db_path = Path(db_path) if db_path else get_default_db_path()
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

            # 4. Persistent Tasks Store (Phase 2)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    task_type TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    started_at REAL,
                    completed_at REAL,
                    parent_task_id TEXT,
                    conversation_id TEXT,
                    exit_code INTEGER,
                    error_message TEXT,
                    artifact_ref TEXT,
                    metadata_json TEXT
                );
            """)
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_conv ON tasks(conversation_id);")

            # 5. Deterministic Workflows Store (Phase 5)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS workflows (
                    run_id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    failure_policy TEXT NOT NULL,
                    start_time REAL NOT NULL,
                    end_time REAL,
                    duration_seconds REAL DEFAULT 0.0,
                    definition_json TEXT NOT NULL,
                    final_artifact_id TEXT,
                    error_message TEXT
                );
            """)
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_wf_status ON workflows(status);")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_wf_conv ON workflows(conversation_id);")

            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS workflow_nodes (
                    run_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    execution_type TEXT NOT NULL,
                    phase INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    task_id TEXT,
                    start_time REAL,
                    end_time REAL,
                    duration_seconds REAL DEFAULT 0.0,
                    result_payload TEXT,
                    artifact_id TEXT,
                    error_message TEXT,
                    PRIMARY KEY (run_id, node_id),
                    FOREIGN KEY (run_id) REFERENCES workflows(run_id) ON DELETE CASCADE
                );
            """)
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_wf_nodes_state ON workflow_nodes(state);")

            # 6. Task Decomposition & Execution Ledger Store (TDEL)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS task_plans (
                    plan_id TEXT PRIMARY KEY,
                    workflow_id TEXT,
                    conversation_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    complexity TEXT NOT NULL,
                    plan_version INTEGER NOT NULL DEFAULT 1,
                    definition_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    completed_at REAL,
                    ledger_artifact_id TEXT,
                    plan_json TEXT NOT NULL,
                    metadata_json TEXT
                );
            """)
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_plans_conv ON task_plans(conversation_id);")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_plans_status ON task_plans(status);")

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

    # ==================== Workflow Store Methods (Phase 5) ====================

    def save_workflow_run(
        self,
        run_id: str,
        workflow_id: str,
        conversation_id: str,
        name: str,
        status: str,
        failure_policy: str,
        start_time: float,
        definition_json: str = "{}",
    ) -> None:
        """Atomically inserts an active workflow run record."""
        with self._conn:
            self._conn.execute("""
                INSERT OR REPLACE INTO workflows (
                    run_id, workflow_id, conversation_id, name, status,
                    failure_policy, start_time, definition_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                run_id, workflow_id, conversation_id, name, status,
                failure_policy, start_time, definition_json
            ))

    def update_workflow_run_status(
        self,
        run_id: str,
        status: str,
        end_time: Optional[float] = None,
        duration_seconds: float = 0.0,
        final_artifact_id: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """Updates the terminal status of a workflow run."""
        with self._conn:
            self._conn.execute("""
                UPDATE workflows
                SET status = ?, end_time = ?, duration_seconds = ?,
                    final_artifact_id = ?, error_message = ?
                WHERE run_id = ?;
            """, (status, end_time, duration_seconds, final_artifact_id, error_message, run_id))

    def save_workflow_node(
        self,
        run_id: str,
        node_id: str,
        name: str,
        execution_type: str,
        phase: int,
        state: str,
        task_id: Optional[str] = None,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
        duration_seconds: float = 0.0,
        result_payload: Optional[Dict[str, Any]] = None,
        artifact_id: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """Persists or updates an individual node execution checkpoint."""
        payload_json = json.dumps(result_payload) if result_payload is not None else None
        with self._conn:
            self._conn.execute("""
                INSERT OR REPLACE INTO workflow_nodes (
                    run_id, node_id, name, execution_type, phase, state,
                    task_id, start_time, end_time, duration_seconds,
                    result_payload, artifact_id, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                run_id, node_id, name, execution_type, phase, state,
                task_id, start_time, end_time, duration_seconds,
                payload_json, artifact_id, error_message
            ))

    def get_workflow_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves a workflow run record along with its node executions."""
        cursor = self._conn.execute("""
            SELECT run_id, workflow_id, conversation_id, name, status,
                   failure_policy, start_time, end_time, duration_seconds,
                   definition_json, final_artifact_id, error_message
            FROM workflows WHERE run_id = ?;
        """, (run_id,))
        row = cursor.fetchone()
        if not row:
            return None

        # Fetch node executions
        node_cursor = self._conn.execute("""
            SELECT node_id, name, execution_type, phase, state,
                   task_id, start_time, end_time, duration_seconds,
                   result_payload, artifact_id, error_message
            FROM workflow_nodes WHERE run_id = ? ORDER BY phase ASC, node_id ASC;
        """, (run_id,))
        nodes = []
        for nr in node_cursor.fetchall():
            res_p = None
            if nr[9]:
                try:
                    res_p = json.loads(nr[9])
                except Exception:
                    pass
            nodes.append({
                "node_id": nr[0],
                "name": nr[1],
                "execution_type": nr[2],
                "phase": nr[3],
                "state": nr[4],
                "task_id": nr[5],
                "start_time": nr[6],
                "end_time": nr[7],
                "duration_seconds": nr[8],
                "result_payload": res_p,
                "artifact_id": nr[10],
                "error_message": nr[11],
            })

        return {
            "run_id": row[0],
            "workflow_id": row[1],
            "conversation_id": row[2],
            "name": row[3],
            "status": row[4],
            "failure_policy": row[5],
            "start_time": row[6],
            "end_time": row[7],
            "duration_seconds": row[8],
            "definition": json.loads(row[9]),
            "final_artifact_id": row[10],
            "error_message": row[11],
            "nodes": nodes,
        }

    def list_workflow_runs(self, conversation_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        """Lists recent workflow executions."""
        if conversation_id:
            cursor = self._conn.execute("""
                SELECT run_id, workflow_id, conversation_id, name, status,
                       failure_policy, start_time, end_time, duration_seconds,
                       final_artifact_id, error_message
                FROM workflows WHERE conversation_id = ? ORDER BY start_time DESC LIMIT ?;
            """, (conversation_id, limit))
        else:
            cursor = self._conn.execute("""
                SELECT run_id, workflow_id, conversation_id, name, status,
                       failure_policy, start_time, end_time, duration_seconds,
                       final_artifact_id, error_message
                FROM workflows ORDER BY start_time DESC LIMIT ?;
            """, (limit,))
        runs = []
        for r in cursor.fetchall():
            runs.append({
                "run_id": r[0],
                "workflow_id": r[1],
                "conversation_id": r[2],
                "name": r[3],
                "status": r[4],
                "failure_policy": r[5],
                "start_time": r[6],
                "end_time": r[7],
                "duration_seconds": r[8],
                "final_artifact_id": r[9],
                "error_message": r[10],
            })
        return runs

    def reconcile_interrupted_workflows(self) -> List[str]:
        """Identifies any workflows left in RUNNING status across unexpected restarts
        and safely marks them INTERRUPTED to prevent zombie executions.
        """
        now = time.time()
        cursor = self._conn.execute("SELECT run_id FROM workflows WHERE status = 'RUNNING';")
        interrupted_ids = [row[0] for row in cursor.fetchall()]
        if interrupted_ids:
            with self._conn:
                for rid in interrupted_ids:
                    self._conn.execute("""
                        UPDATE workflows
                        SET status = 'INTERRUPTED', end_time = ?, error_message = 'Interrupted by unexpected runtime restart.'
                        WHERE run_id = ?;
                    """, (now, rid))
                    self._conn.execute("""
                        UPDATE workflow_nodes
                        SET state = 'INTERRUPTED', end_time = ?, error_message = 'Interrupted by unexpected runtime restart.'
                        WHERE run_id = ? AND state IN ('RUNNING', 'PENDING', 'READY', 'WAITING_DEPENDENCIES');
                    """, (now, rid))
            logger.info(f"Reconciled {len(interrupted_ids)} interrupted workflow runs: {interrupted_ids}")
        return interrupted_ids

    # ==================== Task Plan Ledger Store Methods (TDEL) ====================

    def save_task_plan(
        self,
        plan_id: str,
        conversation_id: str,
        title: str,
        goal: str,
        complexity: str,
        plan_version: int,
        definition_hash: str,
        status: str,
        created_at: float,
        updated_at: float,
        plan_json: str,
        workflow_id: Optional[str] = None,
        completed_at: Optional[float] = None,
        ledger_artifact_id: Optional[str] = None,
        metadata_json: Optional[str] = None,
    ) -> None:
        """Atomically inserts or updates a task plan record."""
        with self._conn:
            self._conn.execute("""
                INSERT OR REPLACE INTO task_plans (
                    plan_id, workflow_id, conversation_id, title, goal,
                    complexity, plan_version, definition_hash, status,
                    created_at, updated_at, completed_at, ledger_artifact_id,
                    plan_json, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                plan_id, workflow_id, conversation_id, title, goal,
                complexity, plan_version, definition_hash, status,
                created_at, updated_at, completed_at, ledger_artifact_id,
                plan_json, metadata_json
            ))

    def update_task_plan_status(
        self,
        plan_id: str,
        status: str,
        updated_at: Optional[float] = None,
        completed_at: Optional[float] = None,
        workflow_id: Optional[str] = None,
        ledger_artifact_id: Optional[str] = None,
    ) -> None:
        """Updates the status and timestamps of an existing task plan."""
        now = updated_at or time.time()
        with self._conn:
            self._conn.execute("""
                UPDATE task_plans
                SET status = ?,
                    updated_at = ?,
                    completed_at = COALESCE(?, completed_at),
                    workflow_id = COALESCE(?, workflow_id),
                    ledger_artifact_id = COALESCE(?, ledger_artifact_id)
                WHERE plan_id = ?;
            """, (status, now, completed_at, workflow_id, ledger_artifact_id, plan_id))

    def get_task_plan(self, plan_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves a single task plan record by plan_id."""
        cursor = self._conn.execute("""
            SELECT plan_id, workflow_id, conversation_id, title, goal,
                   complexity, plan_version, definition_hash, status,
                   created_at, updated_at, completed_at, ledger_artifact_id,
                   plan_json, metadata_json
            FROM task_plans WHERE plan_id = ?;
        """, (plan_id,))
        row = cursor.fetchone()
        if not row:
            return None

        meta = None
        if row[14]:
            try:
                meta = json.loads(row[14])
            except Exception:
                pass

        plan_dict = {}
        if row[13]:
            try:
                plan_dict = json.loads(row[13])
            except Exception:
                pass

        return {
            "plan_id": row[0],
            "workflow_id": row[1],
            "conversation_id": row[2],
            "title": row[3],
            "goal": row[4],
            "complexity": row[5],
            "plan_version": row[6],
            "definition_hash": row[7],
            "status": row[8],
            "created_at": row[9],
            "updated_at": row[10],
            "completed_at": row[11],
            "ledger_artifact_id": row[12],
            "plan_data": plan_dict,
            "metadata": meta,
        }

    def list_task_plans(
        self,
        conversation_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Lists recorded task plans filtered by conversation."""
        if conversation_id:
            cursor = self._conn.execute("""
                SELECT plan_id, workflow_id, conversation_id, title, goal,
                       complexity, plan_version, status, created_at, updated_at,
                       completed_at, ledger_artifact_id
                FROM task_plans
                WHERE conversation_id = ?
                ORDER BY created_at DESC LIMIT ?;
            """, (conversation_id, limit))
        else:
            cursor = self._conn.execute("""
                SELECT plan_id, workflow_id, conversation_id, title, goal,
                       complexity, plan_version, status, created_at, updated_at,
                       completed_at, ledger_artifact_id
                FROM task_plans
                ORDER BY created_at DESC LIMIT ?;
            """, (limit,))

        plans = []
        for r in cursor.fetchall():
            plans.append({
                "plan_id": r[0],
                "workflow_id": r[1],
                "conversation_id": r[2],
                "title": r[3],
                "goal": r[4],
                "complexity": r[5],
                "plan_version": r[6],
                "status": r[7],
                "created_at": r[8],
                "updated_at": r[9],
                "completed_at": r[10],
                "ledger_artifact_id": r[11],
            })
        return plans

    def reconcile_interrupted_plans(self) -> List[str]:
        """Reconciles unclosed plans on startup, marking them INTERRUPTED."""
        now = time.time()
        cursor = self._conn.execute(
            "SELECT plan_id FROM task_plans WHERE status IN ('IN_PROGRESS', 'PENDING');"
        )
        interrupted = [row[0] for row in cursor.fetchall()]
        if interrupted:
            with self._conn:
                for pid in interrupted:
                    self._conn.execute("""
                        UPDATE task_plans
                        SET status = 'INTERRUPTED', updated_at = ?
                        WHERE plan_id = ?;
                    """, (now, pid))
            logger.info(f"Reconciled {len(interrupted)} interrupted task plan(s): {interrupted}")
        return interrupted

    def reconcile_interrupted_tasks(self) -> int:
        """Marks any tasks left in RUNNING, STARTING, or QUEUED as INTERRUPTED on runtime startup."""
        now = time.time()
        with self._conn:
            cur = self._conn.execute("""
                UPDATE tasks
                SET state = 'INTERRUPTED',
                    completed_at = ?,
                    error_message = 'Interrupted by unclean runtime shutdown'
                WHERE state IN ('RUNNING', 'STARTING', 'QUEUED');
            """, (now,))
            return cur.rowcount

    def save_task(self, task: Dict[str, Any]) -> None:
        """Persists or updates a task record in the tasks table."""
        import json
        now = time.time()
        meta_str = json.dumps(task.get("metadata", {})) if isinstance(task.get("metadata"), (dict, list)) else str(task.get("metadata", "{}"))
        with self._conn:
            self._conn.execute("""
                INSERT INTO tasks (
                    task_id, task_type, display_name, state, created_at, started_at,
                    completed_at, parent_task_id, conversation_id, exit_code,
                    error_message, artifact_ref, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    state = excluded.state,
                    started_at = coalesce(excluded.started_at, tasks.started_at),
                    completed_at = coalesce(excluded.completed_at, tasks.completed_at),
                    exit_code = coalesce(excluded.exit_code, tasks.exit_code),
                    error_message = coalesce(excluded.error_message, tasks.error_message),
                    artifact_ref = coalesce(excluded.artifact_ref, tasks.artifact_ref),
                    metadata_json = excluded.metadata_json;
            """, (
                task["task_id"],
                str(task.get("task_type", "TOOL_EXECUTION")),
                str(task.get("display_name", "")),
                str(task.get("state", "QUEUED")),
                float(task.get("created_at", now)),
                task.get("started_at"),
                task.get("completed_at"),
                task.get("parent_task_id"),
                task.get("conversation_id"),
                task.get("exit_code"),
                task.get("error_message"),
                task.get("artifact_ref"),
                meta_str
            ))

    def update_task_state(
        self,
        task_id: str,
        state: str,
        started_at: Optional[float] = None,
        completed_at: Optional[float] = None,
        exit_code: Optional[int] = None,
        error_message: Optional[str] = None,
        artifact_ref: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Updates the lifecycle state and completion metadata for a task."""
        import json
        sets = ["state = ?"]
        params: List[Any] = [state]
        if started_at is not None:
            sets.append("started_at = ?")
            params.append(started_at)
        if completed_at is not None:
            sets.append("completed_at = ?")
            params.append(completed_at)
        if exit_code is not None:
            sets.append("exit_code = ?")
            params.append(exit_code)
        if error_message is not None:
            sets.append("error_message = ?")
            params.append(error_message)
        if artifact_ref is not None:
            sets.append("artifact_ref = ?")
            params.append(artifact_ref)
        if metadata is not None:
            sets.append("metadata_json = ?")
            params.append(json.dumps(metadata))

        params.append(task_id)
        query = f"UPDATE tasks SET {', '.join(sets)} WHERE task_id = ?;"
        with self._conn:
            self._conn.execute(query, tuple(params))

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves a single task record by its task_id."""
        import json
        cur = self._conn.execute("""
            SELECT task_id, task_type, display_name, state, created_at, started_at,
                   completed_at, parent_task_id, conversation_id, exit_code,
                   error_message, artifact_ref, metadata_json
            FROM tasks
            WHERE task_id = ?;
        """, (task_id,))
        row = cur.fetchone()
        if not row:
            return None
        meta = {}
        if row[12]:
            try:
                meta = json.loads(row[12])
            except Exception:
                pass
        return {
            "task_id": row[0],
            "task_type": row[1],
            "display_name": row[2],
            "state": row[3],
            "created_at": row[4],
            "started_at": row[5],
            "completed_at": row[6],
            "parent_task_id": row[7],
            "conversation_id": row[8],
            "exit_code": row[9],
            "error_message": row[10],
            "artifact_ref": row[11],
            "metadata": meta,
        }

    def list_tasks(
        self,
        state: Optional[str] = None,
        task_type: Optional[str] = None,
        conversation_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Lists persisted tasks filtered by state, type, or conversation."""
        import json
        clauses = []
        params: List[Any] = []
        if state:
            clauses.append("state = ?")
            params.append(state)
        if task_type:
            clauses.append("task_type = ?")
            params.append(task_type)
        if conversation_id:
            clauses.append("conversation_id = ?")
            params.append(conversation_id)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"""
            SELECT task_id, task_type, display_name, state, created_at, started_at,
                   completed_at, parent_task_id, conversation_id, exit_code,
                   error_message, artifact_ref, metadata_json
            FROM tasks
            {where_sql}
            ORDER BY created_at DESC
            LIMIT ?;
        """
        params.append(limit)
        cur = self._conn.execute(query, tuple(params))
        results = []
        for row in cur.fetchall():
            meta = {}
            if row[12]:
                try:
                    meta = json.loads(row[12])
                except Exception:
                    pass
            results.append({
                "task_id": row[0],
                "task_type": row[1],
                "display_name": row[2],
                "state": row[3],
                "created_at": row[4],
                "started_at": row[5],
                "completed_at": row[6],
                "parent_task_id": row[7],
                "conversation_id": row[8],
                "exit_code": row[9],
                "error_message": row[10],
                "artifact_ref": row[11],
                "metadata": meta,
            })
        return results

    # ==================== Workflow Store Methods (Phase 5) ====================

    def save_workflow_run(
        self,
        run_id: str,
        workflow_id: str,
        conversation_id: str,
        name: str,
        status: str,
        failure_policy: str,
        start_time: float,
        definition_json: str = "{}",
    ) -> None:
        """Atomically inserts an active workflow run record."""
        with self._conn:
            self._conn.execute("""
                INSERT OR REPLACE INTO workflows (
                    run_id, workflow_id, conversation_id, name, status,
                    failure_policy, start_time, definition_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                run_id, workflow_id, conversation_id, name, status,
                failure_policy, start_time, definition_json
            ))

    def update_workflow_run_status(
        self,
        run_id: str,
        status: str,
        end_time: Optional[float] = None,
        duration_seconds: float = 0.0,
        final_artifact_id: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """Updates the terminal status of a workflow run."""
        with self._conn:
            self._conn.execute("""
                UPDATE workflows
                SET status = ?, end_time = ?, duration_seconds = ?,
                    final_artifact_id = ?, error_message = ?
                WHERE run_id = ?;
            """, (status, end_time, duration_seconds, final_artifact_id, error_message, run_id))

    def save_workflow_node(
        self,
        run_id: str,
        node_id: str,
        name: str,
        execution_type: str,
        phase: int,
        state: str,
        task_id: Optional[str] = None,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
        duration_seconds: float = 0.0,
        result_payload: Optional[Dict[str, Any]] = None,
        artifact_id: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """Persists or updates an individual node execution checkpoint."""
        payload_json = json.dumps(result_payload) if result_payload is not None else None
        with self._conn:
            self._conn.execute("""
                INSERT OR REPLACE INTO workflow_nodes (
                    run_id, node_id, name, execution_type, phase, state,
                    task_id, start_time, end_time, duration_seconds,
                    result_payload, artifact_id, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                run_id, node_id, name, execution_type, phase, state,
                task_id, start_time, end_time, duration_seconds,
                payload_json, artifact_id, error_message
            ))

    def get_workflow_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves a workflow run record along with its node executions."""
        cursor = self._conn.execute("""
            SELECT run_id, workflow_id, conversation_id, name, status,
                   failure_policy, start_time, end_time, duration_seconds,
                   definition_json, final_artifact_id, error_message
            FROM workflows WHERE run_id = ?;
        """, (run_id,))
        row = cursor.fetchone()
        if not row:
            return None

        # Fetch node executions
        node_cursor = self._conn.execute("""
            SELECT node_id, name, execution_type, phase, state,
                   task_id, start_time, end_time, duration_seconds,
                   result_payload, artifact_id, error_message
            FROM workflow_nodes WHERE run_id = ? ORDER BY phase ASC, node_id ASC;
        """, (run_id,))
        nodes = []
        for nr in node_cursor.fetchall():
            res_p = None
            if nr[9]:
                try:
                    res_p = json.loads(nr[9])
                except Exception:
                    pass
            nodes.append({
                "node_id": nr[0],
                "name": nr[1],
                "execution_type": nr[2],
                "phase": nr[3],
                "state": nr[4],
                "task_id": nr[5],
                "start_time": nr[6],
                "end_time": nr[7],
                "duration_seconds": nr[8],
                "result_payload": res_p,
                "artifact_id": nr[10],
                "error_message": nr[11],
            })

        return {
            "run_id": row[0],
            "workflow_id": row[1],
            "conversation_id": row[2],
            "name": row[3],
            "status": row[4],
            "failure_policy": row[5],
            "start_time": row[6],
            "end_time": row[7],
            "duration_seconds": row[8],
            "definition": json.loads(row[9]),
            "final_artifact_id": row[10],
            "error_message": row[11],
            "nodes": nodes,
        }

    def list_workflow_runs(self, conversation_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        """Lists recent workflow executions."""
        if conversation_id:
            cursor = self._conn.execute("""
                SELECT run_id, workflow_id, conversation_id, name, status,
                       failure_policy, start_time, end_time, duration_seconds,
                       final_artifact_id, error_message
                FROM workflows WHERE conversation_id = ? ORDER BY start_time DESC LIMIT ?;
            """, (conversation_id, limit))
        else:
            cursor = self._conn.execute("""
                SELECT run_id, workflow_id, conversation_id, name, status,
                       failure_policy, start_time, end_time, duration_seconds,
                       final_artifact_id, error_message
                FROM workflows ORDER BY start_time DESC LIMIT ?;
            """, (limit,))
        runs = []
        for r in cursor.fetchall():
            runs.append({
                "run_id": r[0],
                "workflow_id": r[1],
                "conversation_id": r[2],
                "name": r[3],
                "status": r[4],
                "failure_policy": r[5],
                "start_time": r[6],
                "end_time": r[7],
                "duration_seconds": r[8],
                "final_artifact_id": r[9],
                "error_message": r[10],
            })
        return runs

    def reconcile_interrupted_workflows(self) -> List[str]:
        """Identifies any workflows left in RUNNING status across unexpected restarts
        and safely marks them INTERRUPTED to prevent zombie executions.
        """
        now = time.time()
        cursor = self._conn.execute("SELECT run_id FROM workflows WHERE status = 'RUNNING';")
        interrupted_ids = [row[0] for row in cursor.fetchall()]
        if interrupted_ids:
            with self._conn:
                for rid in interrupted_ids:
                    self._conn.execute("""
                        UPDATE workflows
                        SET status = 'INTERRUPTED', end_time = ?, error_message = 'Interrupted by unexpected runtime restart.'
                        WHERE run_id = ?;
                    """, (now, rid))
                    self._conn.execute("""
                        UPDATE workflow_nodes
                        SET state = 'INTERRUPTED', end_time = ?, error_message = 'Interrupted by unexpected runtime restart.'
                        WHERE run_id = ? AND state IN ('RUNNING', 'PENDING', 'READY', 'WAITING_DEPENDENCIES');
                    """, (now, rid))
            logger.info(f"Reconciled {len(interrupted_ids)} interrupted workflow runs: {interrupted_ids}")
        return interrupted_ids

    # ==================== Task Plan Ledger Store Methods (TDEL) ====================

    def save_task_plan(
        self,
        plan_id: str,
        conversation_id: str,
        title: str,
        goal: str,
        complexity: str,
        plan_version: int,
        definition_hash: str,
        status: str,
        created_at: float,
        updated_at: float,
        plan_json: str,
        workflow_id: Optional[str] = None,
        completed_at: Optional[float] = None,
        ledger_artifact_id: Optional[str] = None,
        metadata_json: Optional[str] = None,
    ) -> None:
        """Atomically inserts or updates a task plan record."""
        with self._conn:
            self._conn.execute("""
                INSERT OR REPLACE INTO task_plans (
                    plan_id, workflow_id, conversation_id, title, goal,
                    complexity, plan_version, definition_hash, status,
                    created_at, updated_at, completed_at, ledger_artifact_id,
                    plan_json, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                plan_id, workflow_id, conversation_id, title, goal,
                complexity, plan_version, definition_hash, status,
                created_at, updated_at, completed_at, ledger_artifact_id,
                plan_json, metadata_json
            ))

    def update_task_plan_status(
        self,
        plan_id: str,
        status: str,
        updated_at: Optional[float] = None,
        completed_at: Optional[float] = None,
        workflow_id: Optional[str] = None,
        ledger_artifact_id: Optional[str] = None,
    ) -> None:
        """Updates the status and timestamps of an existing task plan."""
        now = updated_at or time.time()
        with self._conn:
            self._conn.execute("""
                UPDATE task_plans
                SET status = ?,
                    updated_at = ?,
                    completed_at = COALESCE(?, completed_at),
                    workflow_id = COALESCE(?, workflow_id),
                    ledger_artifact_id = COALESCE(?, ledger_artifact_id)
                WHERE plan_id = ?;
            """, (status, now, completed_at, workflow_id, ledger_artifact_id, plan_id))

    def get_task_plan(self, plan_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves a single task plan record by plan_id."""
        cursor = self._conn.execute("""
            SELECT plan_id, workflow_id, conversation_id, title, goal,
                   complexity, plan_version, definition_hash, status,
                   created_at, updated_at, completed_at, ledger_artifact_id,
                   plan_json, metadata_json
            FROM task_plans WHERE plan_id = ?;
        """, (plan_id,))
        row = cursor.fetchone()
        if not row:
            return None

        meta = None
        if row[14]:
            try:
                meta = json.loads(row[14])
            except Exception:
                pass

        plan_dict = {}
        if row[13]:
            try:
                plan_dict = json.loads(row[13])
            except Exception:
                pass

        return {
            "plan_id": row[0],
            "workflow_id": row[1],
            "conversation_id": row[2],
            "title": row[3],
            "goal": row[4],
            "complexity": row[5],
            "plan_version": row[6],
            "definition_hash": row[7],
            "status": row[8],
            "created_at": row[9],
            "updated_at": row[10],
            "completed_at": row[11],
            "ledger_artifact_id": row[12],
            "plan_data": plan_dict,
            "metadata": meta,
        }

    def list_task_plans(
        self,
        conversation_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Lists recorded task plans filtered by conversation."""
        if conversation_id:
            cursor = self._conn.execute("""
                SELECT plan_id, workflow_id, conversation_id, title, goal,
                       complexity, plan_version, status, created_at, updated_at,
                       completed_at, ledger_artifact_id
                FROM task_plans
                WHERE conversation_id = ?
                ORDER BY created_at DESC LIMIT ?;
            """, (conversation_id, limit))
        else:
            cursor = self._conn.execute("""
                SELECT plan_id, workflow_id, conversation_id, title, goal,
                       complexity, plan_version, status, created_at, updated_at,
                       completed_at, ledger_artifact_id
                FROM task_plans
                ORDER BY created_at DESC LIMIT ?;
            """, (limit,))

        plans = []
        for r in cursor.fetchall():
            plans.append({
                "plan_id": r[0],
                "workflow_id": r[1],
                "conversation_id": r[2],
                "title": r[3],
                "goal": r[4],
                "complexity": r[5],
                "plan_version": r[6],
                "status": r[7],
                "created_at": r[8],
                "updated_at": r[9],
                "completed_at": r[10],
                "ledger_artifact_id": r[11],
            })
        return plans

    def reconcile_interrupted_plans(self) -> List[str]:
        """Reconciles unclosed plans on startup, marking them INTERRUPTED."""
        now = time.time()
        cursor = self._conn.execute(
            "SELECT plan_id FROM task_plans WHERE status IN ('IN_PROGRESS', 'PENDING');"
        )
        interrupted = [row[0] for row in cursor.fetchall()]
        if interrupted:
            with self._conn:
                for pid in interrupted:
                    self._conn.execute("""
                        UPDATE task_plans
                        SET status = 'INTERRUPTED', updated_at = ?
                        WHERE plan_id = ?;
                    """, (now, pid))
            logger.info(f"Reconciled {len(interrupted)} interrupted task plan(s): {interrupted}")
        return interrupted

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
