"""Win32 MCP Stdio Transport
Asynchronous stream transport bridging native Windows suspended subprocesses
to official MCP Python SDK (ClientSession) memory streams without monkey-patching.

Guarantees:
1. Subprocess is created in CREATE_SUSPENDED state and assigned to a Win32 Job Object before execution.
2. Handle inheritance is restricted strictly to stdio pipes via PROC_THREAD_ATTRIBUTE_HANDLE_LIST.
3. Clean AnyIO memory object streams connect to official ClientSession.
4. Robust bidirectional stdout/stdin stream processing with graceful drain and shutdown.
"""

import sys
import logging
from typing import Optional, Dict, List, Tuple, AsyncGenerator
from contextlib import asynccontextmanager

import anyio
from anyio.streams.file import FileReadStream
from anyio.streams.text import TextReceiveStream

from mcp import types
from mcp.shared.message import SessionMessage

from runtime.platform_win32 import Win32JobObject, Win32SuspendedLauncher, Win32SuspendedProcess

logger = logging.getLogger("presence.mcp.transport")

# Maximum line length permitted for JSON-RPC messages from MCP subprocesses (16 MB)
MAX_MESSAGE_LINE_LENGTH_BYTES = 16 * 1024 * 1024
MAX_STDERR_LINE_LENGTH_BYTES = 64 * 1024


def parse_session_message(line: str) -> SessionMessage | Exception:
    """Parses a single JSON-RPC line into an MCP SessionMessage or returns parse exception."""
    try:
        message = types.jsonrpc_message_adapter.validate_json(line, by_name=False)
        return SessionMessage(message)
    except ValueError as exc:
        logger.exception("Failed to parse JSON-RPC message from MCP server stdout")
        return exc


@asynccontextmanager
async def win32_mcp_stdio_transport(
    command: str,
    args: List[str],
    env: Dict[str, str],
    cwd: Optional[str] = None,
    job: Optional[Win32JobObject] = None,
    encoding: str = "utf-8",
    encoding_error_handler: str = "strict",
    flush_timeout_seconds: float = 2.0,
) -> AsyncGenerator[Tuple[anyio.streams.memory.MemoryObjectReceiveStream, anyio.streams.memory.MemoryObjectSendStream, Win32SuspendedProcess], None]:
    """Spawns an MCP server via Win32SuspendedLauncher and yields (read_stream, write_stream, proc).

    Yields:
        read_stream: MemoryObjectReceiveStream[SessionMessage | Exception] for ClientSession
        write_stream: MemoryObjectSendStream[SessionMessage] for ClientSession
        proc: Win32SuspendedProcess instance
    """
    proc = Win32SuspendedLauncher.launch(
        command=command,
        args=args,
        env=env,
        cwd=cwd,
        job=job,
    )

    read_stream_writer, read_stream = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    write_stream, write_stream_reader = anyio.create_memory_object_stream[SessionMessage](0)
    writer_done = anyio.Event()
    shutting_down = False

    async def stdout_reader() -> None:
        file_stream = FileReadStream(proc.stdout)
        text_stream = TextReceiveStream(file_stream, encoding=encoding, errors=encoding_error_handler)
        buffer = ""
        buffer_byte_count = 0
        try:
            async with read_stream_writer:
                async for chunk in text_stream:
                    chunk_bytes = len(chunk.encode(encoding))
                    if buffer_byte_count + chunk_bytes > MAX_MESSAGE_LINE_LENGTH_BYTES:
                        err = ValueError(f"MCP stdout message line exceeded buffer ceiling ({MAX_MESSAGE_LINE_LENGTH_BYTES} bytes)")
                        logger.error(str(err))
                        await read_stream_writer.send(err)
                        return
                    lines = (buffer + chunk).split("\n")
                    buffer = lines.pop()
                    buffer_byte_count = len(buffer.encode(encoding))
                    for line in lines:
                        clean_line = line.strip("\r")
                        if not clean_line.strip():
                            continue
                        try:
                            await read_stream_writer.send(parse_session_message(clean_line))
                        except (anyio.ClosedResourceError, anyio.BrokenResourceError):
                            return
        except (anyio.ClosedResourceError, anyio.BrokenResourceError):
            pass
        except Exception:
            if not shutting_down:
                logger.exception("Exception in MCP stdout_reader")

    async def stderr_logger() -> None:
        file_stream = FileReadStream(proc.stderr)
        text_stream = TextReceiveStream(file_stream, encoding=encoding, errors="replace")
        buffer = ""
        try:
            async for chunk in text_stream:
                if len(buffer) > MAX_STDERR_LINE_LENGTH_BYTES:
                    logger.warning(f"[MCP-STDERR:{proc.pid}] {buffer[:1024]}... [TRUNCATED OVERSIZED STDERR LINE]")
                    buffer = ""
                lines = (buffer + chunk).split("\n")
                buffer = lines.pop()
                for line in lines:
                    clean_line = line.strip("\r")
                    if clean_line:
                        logger.warning(f"[MCP-STDERR:{proc.pid}] {clean_line[:MAX_STDERR_LINE_LENGTH_BYTES]}")
        except Exception:
            pass

    async def stdin_writer() -> None:
        try:
            async with write_stream_reader:
                async for session_message in write_stream_reader:
                    json_str = session_message.message.model_dump_json(by_alias=True, exclude_unset=True)
                    data = (json_str + "\n").encode(encoding=encoding, errors=encoding_error_handler)
                    await anyio.to_thread.run_sync(proc.stdin.write, data)
        except (anyio.ClosedResourceError, anyio.BrokenResourceError, OSError):
            await read_stream_writer.aclose()
        finally:
            writer_done.set()

    async def shutdown() -> None:
        nonlocal shutting_down
        shutting_down = True
        read_stream.close()
        write_stream.close()
        with anyio.move_on_after(flush_timeout_seconds):
            await writer_done.wait()
        proc.terminate()
        proc.close()

    async with anyio.create_task_group() as tg:
        tg.start_soon(stdout_reader)
        tg.start_soon(stderr_logger)
        tg.start_soon(stdin_writer)
        try:
            yield read_stream, write_stream, proc
        finally:
            with anyio.CancelScope(shield=True):
                await shutdown()
            tg.cancel_scope.cancel()
