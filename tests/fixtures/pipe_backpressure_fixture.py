"""Native MCP Subprocess Pipe Backpressure Fixture
Continuously streams valid newline-delimited JSON-RPC frames over stdout pipe
to stress Windows kernel pipe buffers, AnyIO stream flow control, and reader memory bounds.
Captures per-write latency, kernel pipe block counts, and producer process memory.
"""

import sys
import os
import time
import json
import psutil


def main():
    if len(sys.argv) < 2:
        sys.stderr.write("Usage: pipe_backpressure_fixture.py <target_bytes> [frame_size_bytes]\n")
        sys.exit(1)

    target_bytes = int(sys.argv[1])
    frame_size = int(sys.argv[2]) if len(sys.argv) > 2 else 65536  # Default 64 KiB

    prefix = '{"jsonrpc":"2.0","method":"notifications/stream","params":{"token":'
    suffix = '}}\n'

    bytes_written = 0
    frames_written = 0
    slow_writes_5ms = 0
    slow_writes_10ms = 0
    write_latencies = []
    t_start = time.perf_counter()

    stdout_buf = sys.stdout.buffer

    i = 0
    while bytes_written < target_bytes:
        i += 1
        base_payload = f'{prefix}{i},"data":"'
        remaining_needed = frame_size - len(base_payload) - len(suffix)
        if remaining_needed < 0:
            remaining_needed = 0
        fill = "X" * remaining_needed
        frame_str = f'{base_payload}{fill}"{suffix}'
        frame_bytes = frame_str.encode("utf-8")

        # Measure write + flush latency (time blocked in Windows kernel pipe)
        t_w0 = time.perf_counter()
        try:
            stdout_buf.write(frame_bytes)
            stdout_buf.flush()
        except (BrokenPipeError, OSError):
            break

        t_w1 = time.perf_counter()
        latency_ms = (t_w1 - t_w0) * 1000.0
        write_latencies.append(latency_ms)

        if latency_ms > 5.0:
            slow_writes_5ms += 1
        if latency_ms > 10.0:
            slow_writes_10ms += 1

        bytes_written += len(frame_bytes)
        frames_written += 1

    t_end = time.perf_counter()
    total_duration = t_end - t_start

    # Producer memory metrics
    p = psutil.Process(os.getpid())
    mem = p.memory_info()
    producer_ws_mb = mem.rss / (1024 * 1024)
    producer_priv_mb = getattr(mem, "private", 0) / (1024 * 1024)

    max_lat = max(write_latencies) if write_latencies else 0.0
    avg_lat = sum(write_latencies) / len(write_latencies) if write_latencies else 0.0

    summary = {
        "producer_pid": os.getpid(),
        "target_bytes": target_bytes,
        "bytes_written": bytes_written,
        "frames_written": frames_written,
        "slow_writes_5ms": slow_writes_5ms,
        "slow_writes_10ms": slow_writes_10ms,
        "avg_write_latency_ms": round(avg_lat, 3),
        "max_write_latency_ms": round(max_lat, 3),
        "producer_ws_mb": round(producer_ws_mb, 2),
        "producer_private_mb": round(producer_priv_mb, 2),
        "total_duration_sec": round(total_duration, 4),
        "rate_mb_per_sec": round((bytes_written / (1024 * 1024)) / max(total_duration, 0.0001), 2)
    }

    # Emit final summary notification on stdout
    summary_msg = json.dumps({
        "jsonrpc": "2.0",
        "method": "notifications/summary",
        "params": summary
    }) + "\n"
    try:
        stdout_buf.write(summary_msg.encode("utf-8"))
        stdout_buf.flush()
    except Exception:
        pass


if __name__ == "__main__":
    main()
