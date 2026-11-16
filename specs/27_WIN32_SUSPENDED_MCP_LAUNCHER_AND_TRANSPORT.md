# SPECIFICATION 27: WIN32 SUSPENDED MCP LAUNCHER AND TRANSPORT

**Status**: IMPLEMENTED & VERIFIED  
**Date**: 2026-11-16  
**Subsystem**: Runtime Platform & MCP Subprocess Supervision  
**Parent Milestone**: Phase 2B.1 — Windows Process Containment, MCP Transport Hardening & Security Remediation  
**Governing Architectural Invariant**: *Python remains the Presence application runtime. The official MCP Python SDK owns protocol communication. Native Windows APIs own process creation and containment. Presence owns tool authorization, execution, persistence, and recovery.*

---

## 1. Architectural Problem & Threat Model

In Phase 2B, MCP subprocesses were created using Python's high-level `asyncio.create_subprocess_exec` (via the official MCP SDK `stdio_client`). While `Win32JobObject` assigned the child process to a Windows Job Object after spawn, this introduced two architectural weaknesses:

1. **Post-Spawn Job Assignment Race Condition**:
   Between `CreateProcess` and `AssignProcessToJobObject`, a child process begins executing immediately in the context of the user session. A compromised or adversarial MCP server could immediately spawn detached grandchild processes, allocate unconstrained system memory, or execute breakout payloads before the parent process could call `AssignProcessToJobObject`.
2. **Private SDK Monkey-Patching Fragility**:
   Because `stdio_client` encapsulated subprocess creation internally, Presence patched `mcp.client.stdio._create_platform_compatible_process` via `unittest.mock.patch` to intercept the child process PID. This monkey-patch depended on private SDK internals subject to upstream breaking changes.
3. **Handle Leaks via Unrestricted Handle Inheritance**:
   Standard Windows process spawning with `bInheritHandles = TRUE` can inadvertently leak open parent OS handles (such as SQLite database handles, mutexes, and logging pipes) into untrusted child processes unless explicit handle inheritance filtering is applied.

---

## 2. Windows Native Suspended Launcher Architecture

Presence resolves these vulnerabilities via `Win32SuspendedLauncher` (`runtime/platform_win32.py`), implementing kernel-enforced suspended process creation and explicit handle list filtering.

```
       [ Presence Runtime ]
               │
               ▼
┌─────────────────────────────────────────────────────────────┐
│ 1. Create Anonymous Pipes (stdin, stdout, stderr)          │
│ 2. SetHandleInformation(h_write/h_read, HANDLE_FLAG_INHERIT)│
│ 3. InitializeProcThreadAttributeList (1 attribute)          │
│ 4. UpdateProcThreadAttribute(                               │
│        PROC_THREAD_ATTRIBUTE_HANDLE_LIST,                  │
│        [h_stdin_read, h_stdout_write, h_stderr_write]      │
│    )                                                        │
│ 5. Create Unicode Environment Block (Deduplicated, Sorted)  │
│ 6. CreateProcessW(                                          │
│        CREATE_SUSPENDED |                                   │
│        EXTENDED_STARTUPINFO_PRESENT |                       │
│        CREATE_UNICODE_ENVIRONMENT |                         │
│        CREATE_NO_WINDOW                                     │
│    )                                                        │
└─────────────────────────────────────────────────────────────┘
               │
               ▼ (Process created in kernel; 0 CPU instructions executed)
┌─────────────────────────────────────────────────────────────┐
│ 7. AssignProcessToJobObject(hJob, pi.hProcess)              │
│ 8. IsProcessInJob(pi.hProcess, hJob, &isInJob) -> ASSERTION │
│ 9. ResumeThread(pi.hThread)                                 │
└─────────────────────────────────────────────────────────────┘
               │
               ▼ (Child starts execution strictly inside Job Object boundary)
       [ MCP Server Child Process ]
```

### 2.1 Explicit Handle Inheritance Filtering
Windows Vista and later provide `PROC_THREAD_ATTRIBUTE_HANDLE_LIST` (`0x00020002`).
`Win32SuspendedLauncher` initializes `STARTUPINFOEXW` and updates its attribute list with an explicit 3-element array of handles:
- `h_stdin_read` (Child stdin read pipe)
- `h_stdout_write` (Child stdout write pipe)
- `h_stderr_write` (Child stderr write pipe)

When `CreateProcessW` is invoked with `bInheritHandles = TRUE`, the Windows kernel restricts inheritance strictly to those three handles. All other open handles in Presence (e.g., SQLite WAL handles, mutexes, file logs) are blocked from child inheritance.

### 2.2 Atomic Job Object Containment Before Execution
1. The process is created with `CREATE_SUSPENDED` (`0x00000004`). The primary thread is placed in suspended state at the entry point.
2. `AssignProcessToJobObject` binds `pi.hProcess` to the configured `Win32JobObject` (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`).
3. `IsProcessInJob` queries the kernel to verify membership. If assignment fails or returns false, the child is immediately terminated via `TerminateProcess` and closed without ever running.
4. Once verified, `ResumeThread(pi.hThread)` releases the primary thread. Zero child instructions can run outside the Job Object. Any grandchild processes spawned by the child are automatically captured into the Job Object by the kernel.

### 2.3 Unicode Environment Block
Windows requires that Unicode environment blocks passed to `CreateProcessW` with `CREATE_UNICODE_ENVIRONMENT`:
1. Use UTF-16 encoding (`wchar_t`).
2. Be sorted alphabetically by environment variable name (case-insensitively).
3. Deduplicate keys case-insensitively.
4. Terminate each variable with `\0`, and the entire block with double null `\0\0`.

`create_unicode_environment_block` (`runtime/platform_win32.py`) constructs and validates this binary structure natively.

---

## 3. Asynchronous Stdio Transport Adapter

Rather than monkey-patching `mcp.client.stdio`, Presence implements `win32_mcp_stdio_transport` (`runtime/mcp/transport.py`), an asynchronous context manager connecting `Win32SuspendedProcess` directly to the official MCP SDK `ClientSession`.

### 3.1 AnyIO Memory Object Streams
The official `ClientSession` accepts any standard AnyIO memory streams:
- `read_stream: MemoryObjectReceiveStream[SessionMessage | Exception]`
- `write_stream: MemoryObjectSendStream[SessionMessage]`

`win32_mcp_stdio_transport` creates memory streams and spawns concurrent AnyIO background tasks:
1. `stdout_reader`: Wraps `proc.stdout` with `anyio.streams.file.FileReadStream` and `anyio.streams.text.TextReceiveStream`. It splits incoming chunks into discrete lines and parses them using `mcp.types.jsonrpc_message_adapter.validate_json(line, by_name=False)` into `SessionMessage` instances, delivering them to `read_stream_writer`.
2. `stderr_logger`: Wraps `proc.stderr` asynchronously and logs server diagnostic messages to `presence.mcp.transport` without contaminating the JSON-RPC communication channel.
3. `stdin_writer`: Consumes outgoing `SessionMessage` objects from `write_stream_reader`, serializes them with `.model_dump_json(by_alias=True, exclude_unset=True)`, appends a newline, and writes bytes directly to `proc.stdin`.

### 3.2 Bounded Flush and Deterministic Shutdown
Upon context exit:
1. `read_stream` and `write_stream` are closed, signalling EOF to child and session tasks.
2. A bounded flush window (2.0s) allows pending writer operations to drain.
3. `proc.terminate()` issues `TerminateProcess` and closes the Job Object.
4. `proc.close()` deterministically closes all open file streams, pipe handles, and OS process handles.

---

## 4. Verification and Invariant Assurance

| Security Invariant | Mechanism | Verification Status |
| :--- | :--- | :--- |
| **Zero Breakaway Race** | `CREATE_SUSPENDED` + `AssignProcessToJobObject` | Verified in `tests/test_win32_suspended_launcher.py` |
| **Descendant Containment** | `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` inherits across child + grandchild | Verified with `spawn_tree_fixture.py` (child & grandchild killed) |
| **Handle Isolation** | `PROC_THREAD_ATTRIBUTE_HANDLE_LIST` allowlist | Verified in Windows extended startup info |
| **Zero Monkey-Patching** | Direct AnyIO stream adapter into `ClientSession` | Verified in `tests/test_mcp_transport_adapter.py` |
| **Default-Deny Approval** | `MCPServerConfig.approved_tools` filter | Verified in `tests/test_mcp_tool_approval.py` |
| **Prompt Injection Immunity**| Authority tier assigned solely by local config | Verified in `test_prompt_injection_in_tool_description_cannot_elevate_tier` |
