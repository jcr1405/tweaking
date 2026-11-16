"""Native MCP Subprocess Pipe Backpressure Fixture
Continuously streams valid newline-delimited JSON-RPC frames over stdout pipe
to stress Windows kernel pipe buffers, AnyIO stream flow control, and reader memory bounds.
"""

import sys
import os
import time
import json


def main():
    if len(sys.argv) < 2:
        sys.stderr.write("Usage: pipe_backpressure_fixture.py <target_bytes> [frame_size_bytes]\n")
        sys.exit(1)

    target_bytes = int(sys.argv[1])
    frame_size = int(sys.argv[2]) if len(sys.argv) > 2 else 500

    # Minimum valid JSON-RPC frame structure
    prefix = '{"jsonrpc":"2.0","method":"notifications/stream","params":{"token":'
    suffix = '}}\n'

    bytes_written = 0
    frames_written = 0
    pipe_blocked_events = 0
    t_start = time.perf_counter()

    # Use binary stdout to avoid Python newline translation overhead
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

        # Measure write latency to observe Windows pipe backpressure
        t_w0 = time.perf_counter()
        try:
            stdout_buf.write(frame_bytes)
            stdout_buf.flush()
        except (BrokenPipeError, OSError):
            # Consumer closed pipe (e.g. cancellation)
            break

        t_w1 = time.perf_counter()
        write_duration = t_w1 - t_w0
        # If single flush took > 5ms, Windows pipe buffer was full and write blocked in kernel
        if write_duration > 0.005:
            pipe_blocked_events += 1

        bytes_written += len(frame_bytes)
        frames_written += 1

    t_end = time.perf_counter()
    total_duration = t_end - t_start

    summary = {
        "producer_pid": os.getpid(),
        "target_bytes": target_bytes,
        "bytes_written": bytes_written,
        "frames_written": frames_written,
        "pipe_blocked_events": pipe_blocked_events,
        "total_duration_sec": total_duration,
        "rate_mb_per_sec": (bytes_written / (1024 * 1024)) / max(total_duration, 0.0001)
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
