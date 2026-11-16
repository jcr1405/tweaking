"""Presence Backend Phase 2B.1.3.1 - Resource Telemetry Reconciliation
Executes Experiments A through E with native Win32 process memory, thread, and handle metrics.
"""

import sys
import os
import gc
import time
import json
import psutil
from pathlib import Path

# Add project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runtime.platform_win32 import (
    Win32JobObject,
    Win32SuspendedLauncher,
    get_process_memory_info
)
from runtime.mcp.models import MCPServerConfig
from runtime.mcp.client import MCPClientSession
from runtime.mcp.supervisor import MCPSupervisor
from runtime.mcp.schemas import SchemaCache
from runtime.trajectory import TrajectoryStore
from runtime.conversation import ConversationMetadata
from runtime.journal import EventJournal
from runtime.summaries import GlobalSummaryManager
from runtime.permissions import PermissionBoundary
from runtime.tools.registry import ToolRegistry
from runtime.agent_executor import AgentExecutor, CancellationToken
from runtime.providers.fake import FakeModelProvider


def get_current_metrics(proc: psutil.Process):
    mem = proc.memory_info()
    handles = proc.num_handles()
    threads = proc.num_threads()
    ws_mb = mem.rss / (1024 * 1024)
    private_mb = getattr(mem, "private", 0) / (1024 * 1024)
    return {
        "handles": handles,
        "threads": threads,
        "ws_mb": ws_mb,
        "private_mb": private_mb
    }


def main():
    proc = psutil.Process(os.getpid())
    pid = proc.pid
    name = proc.name()

    echo_fixture = Path("tests/fixtures/echo_server.py")
    if not echo_fixture.exists():
        echo_code = (
            "import sys, json\n"
            "for line in sys.stdin:\n"
            "    req = json.loads(line)\n"
            "    mid = req.get('id')\n"
            "    m = req.get('method')\n"
            "    if m == 'initialize':\n"
            "        resp = {'jsonrpc': '2.0', 'id': mid, 'result': {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'echo', 'version': '1'}}}\n"
            "    elif m == 'tools/list':\n"
            "        resp = {'jsonrpc': '2.0', 'id': mid, 'result': {'tools': [{'name': 'ping', 'description': 'p', 'inputSchema': {'type': 'object'}}]}}\n"
            "    elif m == 'tools/call':\n"
            "        resp = {'jsonrpc': '2.0', 'id': mid, 'result': {'content': [{'type': 'text', 'text': 'pong'}]}}\n"
            "    sys.stdout.write(json.dumps(resp) + chr(10))\n"
            "    sys.stdout.flush()\n"
        )
        echo_fixture.parent.mkdir(parents=True, exist_ok=True)
        echo_fixture.write_text(echo_code, encoding="utf-8")

    cfg = MCPServerConfig(
        server_id="diag_srv",
        name="Diag Server",
        command=sys.executable,
        args=[str(echo_fixture)],
        startup_timeout=3.0,
        request_timeout=2.0
    )

    results = []

    # --- EXPERIMENT A: Runtime Warmup ---
    gc.collect()
    time.sleep(0.1)
    m0_cold = get_current_metrics(proc)

    # First launch (warmup)
    s_warm = MCPClientSession(cfg)
    s_warm.start()
    s_warm.call_tool("ping", {})
    s_warm.stop()
    s_warm = None
    gc.collect()
    time.sleep(0.2)
    m1_warm = get_current_metrics(proc)

    results.append({
        "experiment": "Experiment A: Warmup",
        "scope": f"PID {pid} ({name})",
        "baseline_handles": m0_cold["handles"],
        "final_handles": m1_warm["handles"],
        "handle_delta": m1_warm["handles"] - m0_cold["handles"],
        "baseline_threads": m0_cold["threads"],
        "final_threads": m1_warm["threads"],
        "thread_delta": m1_warm["threads"] - m0_cold["threads"],
        "ws_delta_mb": m1_warm["ws_mb"] - m0_cold["ws_mb"],
        "private_delta_mb": m1_warm["private_mb"] - m0_cold["private_mb"],
        "verdict": "WARMED_UP"
    })

    # --- EXPERIMENT B: 100 Normal MCP Cycles ---
    m_b0 = get_current_metrics(proc)
    for _ in range(100):
        s = MCPClientSession(cfg)
        s.start()
        s.call_tool("ping", {})
        s.stop()
    gc.collect()
    time.sleep(0.3)
    m_b1 = get_current_metrics(proc)

    results.append({
        "experiment": "Experiment B: 100 Normal Cycles",
        "scope": f"PID {pid} ({name})",
        "baseline_handles": m_b0["handles"],
        "final_handles": m_b1["handles"],
        "handle_delta": m_b1["handles"] - m_b0["handles"],
        "baseline_threads": m_b0["threads"],
        "final_threads": m_b1["threads"],
        "thread_delta": m_b1["threads"] - m_b0["threads"],
        "ws_delta_mb": m_b1["ws_mb"] - m_b0["ws_mb"],
        "private_delta_mb": m_b1["private_mb"] - m_b0["private_mb"],
        "verdict": "PASS" if abs(m_b1["handles"] - m_b0["handles"]) <= 5 else "FAIL"
    })

    # --- EXPERIMENT C: Repeated Batch Plateau ---
    m_c0 = get_current_metrics(proc)
    # Batch 1 (15 cycles)
    for _ in range(15):
        s = MCPClientSession(cfg)
        s.start()
        s.call_tool("ping", {})
        s.stop()
    gc.collect()
    time.sleep(0.2)
    m_c1 = get_current_metrics(proc)

    # Batch 2 (25 cycles)
    for _ in range(25):
        s = MCPClientSession(cfg)
        s.start()
        s.call_tool("ping", {})
        s.stop()
    gc.collect()
    time.sleep(0.2)
    m_c2 = get_current_metrics(proc)

    plateau_delta = m_c2["handles"] - m_c1["handles"]
    results.append({
        "experiment": "Experiment C: Batch Plateau (15 -> 25)",
        "scope": f"PID {pid} ({name})",
        "baseline_handles": m_c1["handles"],
        "final_handles": m_c2["handles"],
        "handle_delta": plateau_delta,
        "baseline_threads": m_c1["threads"],
        "final_threads": m_c2["threads"],
        "thread_delta": m_c2["threads"] - m_c1["threads"],
        "ws_delta_mb": m_c2["ws_mb"] - m_c1["ws_mb"],
        "private_delta_mb": m_c2["private_mb"] - m_c1["private_mb"],
        "verdict": "PASS (PLATEAU DELTA=0)" if plateau_delta == 0 else f"DELTA={plateau_delta}"
    })

    # --- EXPERIMENT D: Full Runtime Lifecycle (25 cycles) ---
    import tempfile
    td = tempfile.mkdtemp(prefix="diag_d_")
    db_dir = Path(td) / "dbs"
    logs_dir = Path(td) / "logs"
    sum_db = Path(td) / "sum.db"

    m_d0 = get_current_metrics(proc)
    for i in range(25):
        cid = f"diag_conv_{i}"
        store = TrajectoryStore(conversation_id=cid, db_dir=db_dir)
        meta = ConversationMetadata(conversation_id=cid, title=f"Diag {i}")
        store.save_metadata(meta)
        j = EventJournal(conversation_id=cid, logs_dir=logs_dir)
        sm = GlobalSummaryManager(db_path=sum_db)
        b = PermissionBoundary()
        r = ToolRegistry()
        p_fake = FakeModelProvider()
        ex = AgentExecutor(
            trajectory_store=store,
            event_journal=j,
            summary_manager=sm,
            permission_boundary=b,
            tool_registry=r,
            model_provider=p_fake
        )
        list(ex.execute_turn(user_input="Test", cancellation_token=CancellationToken()))
        store.close()
        j.close()
        sm.close()

    gc.collect()
    time.sleep(0.2)
    m_d1 = get_current_metrics(proc)
    import shutil
    shutil.rmtree(td, ignore_errors=True)

    results.append({
        "experiment": "Experiment D: Full Runtime (25 cyc)",
        "scope": f"PID {pid} ({name})",
        "baseline_handles": m_d0["handles"],
        "final_handles": m_d1["handles"],
        "handle_delta": m_d1["handles"] - m_d0["handles"],
        "baseline_threads": m_d0["threads"],
        "final_threads": m_d1["threads"],
        "thread_delta": m_d1["threads"] - m_d0["threads"],
        "ws_delta_mb": m_d1["ws_mb"] - m_d0["ws_mb"],
        "private_delta_mb": m_d1["private_mb"] - m_d0["private_mb"],
        "verdict": "PASS" if abs(m_d1["handles"] - m_d0["handles"]) <= 5 else "FAIL"
    })

    # Print JSON output for reporting
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
