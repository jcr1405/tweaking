# Specification 31: Phase 2B.1 Remediation Changelog

## 1. Overview

This document records the exact, targeted code modifications applied to the Presence Runtime during **Phase 2B.1.1 Stage B (Controlled Remediation)** to resolve the vulnerabilities and edge-case defects identified in **Specification 30**.

All changes conform strictly to the governing architectural invariant:
> Python remains the Presence application runtime. The official MCP Python SDK owns protocol communication. Native Windows APIs own process creation and containment. Presence owns tool authorization, execution, persistence and recovery.

No third-party libraries were added. No Rust was introduced. No core architectural abstractions were refactored.

---

## 2. Modified Production Files & Remediation Details

### 2.1 `runtime/platform_win32.py`
**Defects Addressed**: `FINDING-ADV-01`, `FINDING-ADV-05`

#### Changes:
1. **Configured Explicit 64-bit ctypes Signatures (`FINDING-ADV-05`)**:
   Configured `.argtypes` and `.restype` on `CreatePipe`, `SetHandleInformation`, `AssignProcessToJobObject`, `IsProcessInJob`, `ResumeThread`, and `TerminateProcess`. This eliminates implicit 32-bit int marshaling hazards on 64-bit Windows.
   ```python
   kernel32.CreatePipe.argtypes = [ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE), ctypes.c_void_p, wintypes.DWORD]
   kernel32.CreatePipe.restype = wintypes.BOOL
   kernel32.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
   kernel32.SetHandleInformation.restype = wintypes.BOOL
   kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
   kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
   kernel32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
   kernel32.IsProcessInJob.restype = wintypes.BOOL
   kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
   kernel32.ResumeThread.restype = wintypes.DWORD
   kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
   kernel32.TerminateProcess.restype = wintypes.BOOL
   ```

2. **Enforced Fail-Closed Verification on `IsProcessInJob` (`FINDING-ADV-01`)**:
   Replaced the conditional `if kernel32.IsProcessInJob(...): if not is_in_job: ...` block with strict fail-closed verification. If `IsProcessInJob` returns 0 (API failure) or if `is_in_job.value` is 0 (process is not in job), the suspended process is immediately terminated, handles are closed, and a `RuntimeError` is raised.
   ```python
   # 10. Verify Assignment using IsProcessInJob (Fail-Closed)
   is_in_job = wintypes.BOOL()
   call_success = kernel32.IsProcessInJob(pi.hProcess, active_job.handle, ctypes.byref(is_in_job))
   if not call_success or not is_in_job.value:
       err = kernel32.GetLastError()
       kernel32.TerminateProcess(pi.hProcess, 1)
       kernel32.CloseHandle(pi.hThread)
       kernel32.CloseHandle(pi.hProcess)
       kernel32.CloseHandle(h_stdin_write)
       kernel32.CloseHandle(h_stdout_read)
       kernel32.CloseHandle(h_stderr_read)
       if not call_success:
           raise RuntimeError(
               f"Security invariant violated: IsProcessInJob verification failed for PID {pi.dwProcessId} "
               f"(Win32 error {err}). Process terminated safely."
           )
       else:
           raise RuntimeError(
               f"Security invariant violated: Process {pi.dwProcessId} is not member of Job Object '{active_job.name}'. Terminated."
           )
   ```

---

### 2.2 `runtime/mcp/supervisor.py`
**Defects Addressed**: `FINDING-ADV-02`, `FINDING-ADV-04`

#### Changes:
1. **Added Per-Server Re-Entrant Lifecycle Synchronization (`FINDING-ADV-04`)**:
   Added `self._server_locks: Dict[str, threading.RLock] = {}` and `_get_server_lock(server_id)` helper. Wrapped `start_server`, `stop_server`, `restart_server`, and `execute_tool` in `with self._get_server_lock(server_id):`. This eliminates race conditions when multiple threads concurrently attempt to start or communicate with an MCP server.
   ```python
   def _get_server_lock(self, server_id: str) -> threading.RLock:
       """Returns or creates a re-entrant lock for the specified server."""
       with self._global_lock:
           if server_id not in self._server_locks:
               self._server_locks[server_id] = threading.RLock()
           return self._server_locks[server_id]
   ```

2. **Eliminated Blind Non-Idempotent Tool Replay on Process Crash (`FINDING-ADV-02`)**:
   Removed the blind retry logic (`session.call_tool(...)`) and removed the test-specific bypass `if tool_name != "trigger_crash":`. When a server crashes during tool execution, `restart_server` is invoked to restore availability for future calls, but the current execution returns an explicit error envelope without re-invoking the mutating tool.
   ```python
   # Check for crash during execution
   if result.get("status") == "ERROR" and (not session.is_alive() or session.state != MCPServerState.READY):
       logger.warning(f"MCP server '{server_id}' crashed or exited during execution of tool '{tool_name}'.")
       # Restart server in background to restore availability for subsequent calls
       self.restart_server(server_id)
       # Fail closed: Do NOT blindly retry non-idempotent tool mutations on crash
       return {
           "status": "ERROR",
           "error": f"MCP server '{server_id}' crashed or terminated during execution of tool '{tool_name}'. Tool execution was NOT replayed to prevent non-idempotent mutation hazards.",
           "server_id": server_id,
           "tool": tool_name,
           "server_crashed": True
       }
   ```

---

### 2.3 `runtime/permissions.py`
**Defects Addressed**: `FINDING-ADV-06`

#### Changes:
1. **Added Cryptographic Single-Use Consent Token Architecture**:
   Introduced `IssuedConsent` dataclass and methods `issue_consent_token` and `verify_consent_token` on `PermissionBoundary`. Tokens are generated using `secrets.token_urlsafe(32)` with configurable TTL (default 60s) and bound to the specific action name. Once verified, the token is popped immediately (single-use consumption), defeating replay attacks.
   ```python
   @dataclass(frozen=True)
   class IssuedConsent:
       token: str
       action_name: str
       expires_at: float
       context_keys: tuple

   def issue_consent_token(
       self,
       action_name: str,
       context: Optional[Dict[str, Any]] = None,
       ttl_seconds: float = 60.0
   ) -> str:
       """Issues a single-use cryptographically random consent token for an action."""
       token = f"cst_{secrets.token_urlsafe(32)}"
       context_keys = tuple(sorted(context.keys())) if context else ()
       self._active_consent_tokens[token] = IssuedConsent(
           token=token,
           action_name=action_name,
           expires_at=time.time() + ttl_seconds,
           context_keys=context_keys
       )
       logger.info(f"Issued single-use consent token for '{action_name}' (TTL: {ttl_seconds}s)")
       return token

   def verify_consent_token(
       self,
       token: str,
       action_name: str,
       context: Optional[Dict[str, Any]] = None
   ) -> bool:
       """Verifies and consumes a single-use consent token. Returns True if valid, False otherwise."""
       if not token or not isinstance(token, str):
           return False

       consent = self._active_consent_tokens.pop(token, None)
       if not consent:
           logger.warning(f"Invalid or already-consumed consent token presented for action '{action_name}'.")
           return False

       now = time.time()
       if now > consent.expires_at:
           logger.warning(f"Expired consent token presented for action '{action_name}' (expired {now - consent.expires_at:.1f}s ago).")
           return False

       if consent.action_name != action_name:
           logger.warning(f"Consent token action mismatch: expected '{consent.action_name}', got '{action_name}'.")
           return False

       logger.info(f"Verified and consumed valid consent token for '{action_name}'.")
       return True
   ```

---

### 2.4 `runtime/tools/registry.py`
**Defects Addressed**: `FINDING-ADV-06`

#### Changes:
1. **Replaced Blind `bool(consent_token)` Evaluation with Formal Verification**:
   Replaced `granted_consent=bool(consent_token)` with a call to `permission_boundary.verify_consent_token(token, action_name, args)`. Unverified or forged tokens fail verification, ensuring the L3 security gate remains impenetrable.
   ```python
   # Validate explicit user consent token if provided (cryptographic single-use verification)
   granted_consent = False
   if consent_token:
       granted_consent = permission_boundary.verify_consent_token(
           token=consent_token,
           action_name=tool.tool_id,
           context=args
       )

   # Authorize against the 4-tier permission boundary
   auth: AuthorizationResult = permission_boundary.check_permission(
       action_name=tool.tool_id,
       context=args,
       granted_consent=granted_consent
   )
   ```

---

### 2.5 `runtime/artifacts/store.py`
**Defects Addressed**: `FINDING-ADV-03`

#### Changes:
1. **Added Thread-Safe Conversation Quota Locking**:
   Added `self._locks: Dict[str, threading.Lock] = {}` and `_get_conversation_lock(conversation_id)`. Wrapped quota evaluation (`get_conversation_storage_usage`) and atomic artifact writing inside `with self._get_conversation_lock(conversation_id):`. This eliminates race conditions during concurrent writes.

---

### 2.6 `.gitignore`
**Defects Addressed**: `FINDING-ADV-07`

#### Changes:
1. **Scoped App-Data Artifact Directory Exclusion**:
   Changed `.gitignore` entry `artifacts/` to `/artifacts/` and added `!runtime/artifacts/` exception. This ensures that the local data directory is ignored without hiding the production package `runtime/artifacts/` from git tracking.

---

## 3. Newly Authored Adversarial Test Suites (`tests/adversarial/`)

| Test File | Test Count | Focus Area |
| :--- | :--- | :--- |
| `tests/adversarial/test_adv_win32_launcher.py` | 5 | Suspended launch, Job membership, handle leaks, `IsProcessInJob` fail-closed |
| `tests/adversarial/test_adv_job_containment.py` | 2 | Parent kill tree-reaping, `CREATE_BREAKAWAY_FROM_JOB` defense |
| `tests/adversarial/test_adv_handle_security.py` | 1 | `PROC_THREAD_ATTRIBUTE_HANDLE_LIST` inheritance isolation |
| `tests/adversarial/test_adv_mcp_transport.py` | 3 | 1-byte chunk reassembly, multi-message frames, stderr noise isolation |
| `tests/adversarial/test_adv_supervisor_lifecycle.py` | 4 | Max restart limits, non-idempotent crash safety, concurrent tool execution |
| `tests/adversarial/test_adv_artifact_storage.py` | 5 | 40 KB exact boundary, multibyte UTF-8 safety, path traversal, quota races |
| `tests/adversarial/test_adv_repeated_lifecycle.py` | 1 | 30 repeated MCP spawn/terminate cycles; 0 handle, thread, or memory leaks |
| `tests/adversarial/test_adv_persistence.py` | 3 | SQLite WAL durability, monotonic indexing, corrupt JSONL line recovery |
| `tests/adversarial/test_adv_agent_executor.py` | 3 | Provider network disconnect, infinite tool loop cap, unauthorized L3 handling |
| `tests/adversarial/test_adv_permissions_security.py` | 3 | Prompt injection defense, default-deny unapproved tools, consent token security |
| **Total Adversarial Tests** | **30** | **All 30 Passing (100%)** |

---

## 4. Verification and Regression Test Confirmation

### 4.1 Adversarial Test Execution
```
Ran 30 tests in 52.522s
OK
[Telemetry Baseline] Handles: 252, Threads: 9, WS: 101.58 MB
[Telemetry After 30 Cycles] Handles: 252 (Delta: +0), Threads: 9 (Delta: +0), WS: 102.12 MB (Delta: +0.54 MB)
```

### 4.2 Baseline Regression Test Execution
```
Ran 107 tests in 17.787s
OK (skipped=2)
Total Tests Run:           107
Total Passed:             105
Total Failures:           0
Total Errors:             0
Total Skipped:            2
Idle Runtime Working Set: 96.27 MB (Target: < 100.0 MB -> PASS)
Test Suite Peak WS:       113.45 MB
```

### 4.3 Summary
- Zero baseline test regressions.
- All 137 tests across the codebase discover and execute cleanly.
- Idle runtime memory remains bounded at 96.27 MB.
