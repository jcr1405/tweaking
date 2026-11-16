"""Executes the complete 3x3 Real Subprocess Pipe Backpressure Matrix
Volumes: 1 MB, 10 MB, 50 MB
Consumer Delays: 0.0 ms (fast), 1.0 ms (slow), 10.0 ms (throttled)
"""

import sys
import os
import time
import json
from pathlib import Path

# Add project root
root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))

import anyio
import psutil
from runtime.platform_win32 import Win32JobObject, get_process_memory_info
from runtime.mcp.transport import win32_mcp_stdio_transport

fixture_path = str(root / "tests" / "fixtures" / "pipe_backpressure_fixture.py")
env = {"SystemRoot": os.environ.get("SystemRoot", r"C:\Windows")}


async def execute_cell(target_bytes: int, consumer_delay_sec: float, frame_size: int = 65536):
    job = Win32JobObject(name="TestBPMatrixJob")
    job.create(kill_on_close=True)

    presence_proc = psutil.Process(os.getpid())
    presence_mem_0 = presence_proc.memory_info()
    p_ws_0 = presence_mem_0.rss / (1024 * 1024)
    p_priv_0 = getattr(presence_mem_0, "private", 0) / (1024 * 1024)

    frames_consumed = 0
    summary = None
    t0 = time.perf_counter()

    async with win32_mcp_stdio_transport(
        command=sys.executable,
        args=[fixture_path, str(target_bytes), str(frame_size)],
        env=env,
        job=job
    ) as (read_stream, write_stream, proc):
        async for msg in read_stream:
            frames_consumed += 1
            if hasattr(msg, "message") and hasattr(msg.message, "root"):
                root_obj = msg.message.root
                if getattr(root_obj, "method", None) == "notifications/summary":
                    summary = root_obj.params
            elif hasattr(msg, "message") and getattr(msg.message, "method", None) == "notifications/summary":
                summary = msg.message.params

            if consumer_delay_sec > 0:
                await anyio.to_thread.run_sync(time.sleep, consumer_delay_sec)
            if isinstance(msg, Exception):
                break

    t1 = time.perf_counter()
    presence_mem_1 = presence_proc.memory_info()
    p_ws_1 = presence_mem_1.rss / (1024 * 1024)
    p_priv_1 = getattr(presence_mem_1, "private", 0) / (1024 * 1024)

    job.close()
    consumer_duration = t1 - t0

    return {
        "volume_bytes": target_bytes,
        "volume_label": f"{target_bytes // (1024 * 1024)} MB",
        "delay_ms": consumer_delay_sec * 1000.0,
        "delay_label": "Fast (0ms)" if consumer_delay_sec == 0 else f"{int(consumer_delay_sec * 1000)}ms",
        "frames_consumed": frames_consumed,
        "consumer_duration_sec": round(consumer_duration, 4),
        "presence_ws_start_mb": round(p_ws_0, 2),
        "presence_ws_end_mb": round(p_ws_1, 2),
        "presence_ws_delta_mb": round(p_ws_1 - p_ws_0, 2),
        "presence_priv_delta_mb": round(p_priv_1 - p_priv_0, 2),
        "producer_summary": summary
    }


def main():
    volumes = [1 * 1024 * 1024, 10 * 1024 * 1024, 50 * 1024 * 1024]
    delays = [0.0, 0.001, 0.010]  # Fast, 1ms, 10ms

    matrix_results = []
    print("=" * 75)
    print("  RUNNING 3x3 REAL SUBPROCESS PIPE BACKPRESSURE MATRIX")
    print("=" * 75)

    for vol in volumes:
        for d in delays:
            vol_mb = vol // (1024 * 1024)
            d_ms = int(d * 1000)
            print(f"Testing {vol_mb} MB with {d_ms} ms consumer delay...", end=" ", flush=True)
            res = anyio.run(execute_cell, vol, d, 65536)
            matrix_results.append(res)
            prod = res["producer_summary"] or {}
            print(f"Done. Pres WS Delta: {res['presence_ws_delta_mb']:+.2f} MB | Prod Rate: {prod.get('rate_mb_per_sec', 0)} MB/s | Slow writes (>5ms): {prod.get('slow_writes_5ms', 0)}")

    out_file = root / "specs" / "backpressure_3x3_matrix.json"
    out_file.write_text(json.dumps(matrix_results, indent=2), encoding="utf-8")
    print(f"\nSaved complete matrix to {out_file}")


if __name__ == "__main__":
    main()
