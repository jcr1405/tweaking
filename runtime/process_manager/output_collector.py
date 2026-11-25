"""Process Output Stream Collector
Streams stdout and stderr directly to persistent disk artifacts without unbounded RAM accumulation.
Maintains bounded memory previews and safe reverse-seek log tailing to defeat OS pipe deadlocks.
"""

import os
import io
import time
import threading
import logging
from typing import Optional, Dict, Any, List

logger = logging.getLogger("presence.process_manager.collector")


class ProcessOutputCollector:
    """Thread-safe background stream collector that drains process pipes directly to disk.
    
    Guarantees:
    1. Immunity to OS pipe deadlocks: Reader threads drain pipes continuously.
    2. Zero OOM hazards: Full output streams to disk; memory stores only bounded preview chunks.
    3. Thread-safe tail queries: Reads recent lines from disk via reverse seek.
    """

    def __init__(
        self,
        stdout_pipe: Any,
        stderr_pipe: Any,
        stdout_path: str,
        stderr_path: str,
        max_preview_bytes: int = 16384,
    ):
        self.stdout_pipe = stdout_pipe
        self.stderr_pipe = stderr_pipe
        self.stdout_path = stdout_path
        self.stderr_path = stderr_path
        self.max_preview_bytes = max_preview_bytes

        self._lock = threading.Lock()
        self._stdout_preview_chunks: List[str] = []
        self._stderr_preview_chunks: List[str] = []
        self._stdout_bytes_collected: int = 0
        self._stderr_bytes_collected: int = 0
        self._stdout_preview_len: int = 0
        self._stderr_preview_len: int = 0

        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._is_active = False

    def start(self) -> None:
        """Starts background reader threads for stdout and stderr."""
        with self._lock:
            if self._is_active:
                return
            self._is_active = True

            # Ensure parent directories exist
            os.makedirs(os.path.dirname(os.path.abspath(self.stdout_path)), exist_ok=True)
            os.makedirs(os.path.dirname(os.path.abspath(self.stderr_path)), exist_ok=True)

            self._stdout_thread = threading.Thread(
                target=self._drain_stream,
                args=(self.stdout_pipe, self.stdout_path, "stdout"),
                daemon=True,
                name=f"Collector-stdout-{os.path.basename(self.stdout_path)}"
            )
            self._stderr_thread = threading.Thread(
                target=self._drain_stream,
                args=(self.stderr_pipe, self.stderr_path, "stderr"),
                daemon=True,
                name=f"Collector-stderr-{os.path.basename(self.stderr_path)}"
            )
            self._stdout_thread.start()
            self._stderr_thread.start()

    def _drain_stream(self, pipe: Any, file_path: str, stream_name: str) -> None:
        """Continuously reads from stream pipe and writes to disk."""
        if not pipe:
            return

        try:
            with open(file_path, "ab", buffering=0) as f:
                while not self._stop_event.is_set():
                    try:
                        # Read raw chunk
                        chunk = pipe.read(4096)
                        if not chunk:
                            break  # Pipe reached EOF

                        # Write directly to disk artifact
                        f.write(chunk)

                        # Update bounded memory preview
                        chunk_str = chunk.decode("utf-8", errors="replace")
                        with self._lock:
                            if stream_name == "stdout":
                                self._stdout_bytes_collected += len(chunk)
                                if self._stdout_preview_len < self.max_preview_bytes:
                                    self._stdout_preview_chunks.append(chunk_str)
                                    self._stdout_preview_len += len(chunk_str)
                            else:
                                self._stderr_bytes_collected += len(chunk)
                                if self._stderr_preview_len < self.max_preview_bytes:
                                    self._stderr_preview_chunks.append(chunk_str)
                                    self._stderr_preview_len += len(chunk_str)

                    except (ValueError, OSError, io.UnsupportedOperation):
                        break
                    except Exception as e:
                        logger.debug(f"Collector read error on {stream_name}: {e}")
                        break

        except Exception as file_err:
            logger.error(f"Failed writing {stream_name} to {file_path}: {file_err}")

    def stop(self) -> None:
        """Signals reader threads to stop."""
        self._stop_event.set()

    def wait_completion(self, timeout: float = 2.0) -> None:
        """Waits for reader threads to finish draining pipes up to timeout seconds."""
        threads = [t for t in (self._stdout_thread, self._stderr_thread) if t and t.is_alive()]
        end_time = time.time() + timeout
        for t in threads:
            remaining = max(0.01, end_time - time.time())
            t.join(timeout=remaining)

    def get_stdout_preview(self, max_chars: int = 4000) -> str:
        """Returns buffered stdout text preview."""
        with self._lock:
            full = "".join(self._stdout_preview_chunks)
            if len(full) > max_chars:
                return full[:max_chars] + f"\n[... Output truncated: {self._stdout_bytes_collected} total bytes stored in artifact ...]"
            return full

    def get_stderr_preview(self, max_chars: int = 4000) -> str:
        """Returns buffered stderr text preview."""
        with self._lock:
            full = "".join(self._stderr_preview_chunks)
            if len(full) > max_chars:
                return full[:max_chars] + f"\n[... Stderr truncated: {self._stderr_bytes_collected} total bytes stored in artifact ...]"
            return full

    @property
    def total_bytes_collected(self) -> int:
        """Returns sum of stdout and stderr bytes captured."""
        with self._lock:
            return self._stdout_bytes_collected + self._stderr_bytes_collected

    def read_tail(self, stream: str = "stdout", max_lines: int = 50) -> str:
        """Safely reads the last N lines from the disk artifact using reverse seek.
        Does not load the whole file into RAM.
        """
        file_path = self.stdout_path if stream == "stdout" else self.stderr_path
        if not os.path.exists(file_path):
            return ""

        file_size = os.path.getsize(file_path)
        if file_size == 0:
            return ""

        # Reverse seek algorithm to read last N lines
        buffer_size = 8192
        lines: List[bytes] = []
        with open(file_path, "rb") as f:
            if file_size <= buffer_size:
                f.seek(0)
                content = f.read()
                return content.decode("utf-8", errors="replace")

            pos = file_size
            remainder = b""
            while pos > 0 and len(lines) <= max_lines:
                read_len = min(pos, buffer_size)
                pos -= read_len
                f.seek(pos)
                chunk = f.read(read_len) + remainder
                parts = chunk.split(b"\n")
                remainder = parts[0]
                lines = parts[1:] + lines

            result_bytes = b"\n".join(lines[-max_lines:])
            return result_bytes.decode("utf-8", errors="replace")
