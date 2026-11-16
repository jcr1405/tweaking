"""Empirical Performance Benchmarks for Phase 1 Subsystems"""

import sys
import time
import tempfile
from pathlib import Path

# Add project root to sys.path
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from runtime.conversation import ConversationMetadata
from runtime.trajectory import TrajectoryStore, Step, StepType, StepStatus
from runtime.journal import EventJournal
from runtime.summaries import GlobalSummaryManager
from runtime.reconstruction import ContextAssembler
from runtime.platform_win32 import get_process_memory_info


def run_benchmarks():
    print("=" * 70)
    print("  PHASE 1 SUBSYSTEM PERFORMANCE BENCHMARKS")
    print("=" * 70)

    temp_dir = tempfile.TemporaryDirectory()
    base_dir = Path(temp_dir.name)
    mem_start = get_process_memory_info()

    # 1. Benchmark: Conversation Creation Latency
    t0 = time.perf_counter()
    conv_id = "bench-conv-001"
    meta = ConversationMetadata(
        conversation_id=conv_id,
        title="Benchmark Session",
        workspace_path=r"C:\benchmark"
    )
    store = TrajectoryStore(conversation_id=conv_id, db_dir=base_dir / "conversations")
    store.save_metadata(meta)
    conv_create_time_ms = (time.perf_counter() - t0) * 1000.0

    # 2. Benchmark: Step Persistence Latency (100 sequential steps)
    step_times = []
    journal = EventJournal(conversation_id=conv_id, logs_dir=base_dir / "logs")
    for i in range(100):
        step = Step(
            idx=i,
            step_type=StepType.USER_INPUT if i % 2 == 0 else StepType.AGENT_TURN,
            status=StepStatus.DONE,
            step_payload={"content": f"Turn payload {i} with reasoning content and parameters."}
        )
        t_step = time.perf_counter()
        store.append_step(step)
        journal.append_step_event(step)
        step_times.append((time.perf_counter() - t_step) * 1000.0)

    avg_step_persist_ms = sum(step_times) / len(step_times)
    p95_step_persist_ms = sorted(step_times)[int(len(step_times) * 0.95)]

    # 3. Benchmark: FTS5 Full-Text Search Indexing & Query Latency
    summary_mgr = GlobalSummaryManager(db_path=base_dir / "summaries.db")
    t_idx0 = time.perf_counter()
    for i in range(100):
        summary_mgr.index_step_text(
            conversation_id=conv_id,
            step_idx=i,
            title="Benchmark Session",
            workspace_path=r"C:\benchmark",
            user_body=f"Query {i} discussing SQLite WAL mode and FTS5 indexing."
        )
    index_time_100_ms = (time.perf_counter() - t_idx0) * 1000.0

    query_times = []
    for term in ["SQLite", "WAL", "FTS5", "indexing", "Query"]:
        t_q = time.perf_counter()
        results = summary_mgr.search_conversations(term, limit=10)
        query_times.append((time.perf_counter() - t_q) * 1000.0)
    avg_query_time_ms = sum(query_times) / len(query_times)

    # 4. Benchmark: Deterministic Context Reconstruction Latency (100 steps)
    assembler = ContextAssembler(
        trajectory_store=store,
        summary_manager=summary_mgr,
        max_context_tokens=8192
    )
    t_recon = time.perf_counter()
    ctx = assembler.assemble(
        system_instructions="You are an autonomous desktop companion.",
        desktop_ambient_context={"active_window": "VS Code", "cpu_load": 1.2}
    )
    recon_time_ms = (time.perf_counter() - t_recon) * 1000.0

    mem_end = get_process_memory_info()

    store.close()
    journal.close()
    summary_mgr.close()
    temp_dir.cleanup()

    print(f"Conversation Creation Latency:       {conv_create_time_ms:.3f} ms")
    print(f"Average Dual-Journal Persist:        {avg_step_persist_ms:.3f} ms / step")
    print(f"P95 Dual-Journal Persist:            {p95_step_persist_ms:.3f} ms / step")
    print(f"FTS5 100-Step Indexing Latency:      {index_time_100_ms:.3f} ms total")
    print(f"Average FTS5 Query Latency:          {avg_query_time_ms:.3f} ms / query")
    print(f"Context Reconstruction (100 steps):  {recon_time_ms:.3f} ms")
    print(f"Final Working Set Memory:            {mem_end.working_set_mb:.2f} MB (Delta: +{mem_end.working_set_mb - mem_start.working_set_mb:.2f} MB)")
    print(f"Final Private Bytes:                 {mem_end.private_mb:.2f} MB")
    print("=" * 70)


if __name__ == "__main__":
    run_benchmarks()
