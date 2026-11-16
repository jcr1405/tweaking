# Specification 34: Phase 2B.1.2 Remediation Changelog

## 1. Overview

This document records the exact, targeted code modifications applied to the Presence Runtime during **Phase 2B.1.2 (Security Gap Closure & Final Adversarial Verification)**.

All modifications strictly maintain the governing architectural invariant:
> Python remains the Presence application runtime. The official MCP Python SDK owns protocol communication. Native Windows APIs own process creation and containment. Presence owns tool authorization, execution, persistence and recovery.

No third-party libraries were added. No Rust was introduced. No core architectural abstractions were replaced.

---

## 2. Modified Production Files & Remediation Details

### 2.1 `runtime/permissions.py`
**Defect Addressed**: `FINDING-ADV-08` (Critical — L3 Consent Token Parameter Tampering)

#### Changes:
1. **Implemented Canonical Argument Serialization**:
   Added `canonicalize_arguments(args: Dict[str, Any]) -> str`:
   - Validates that dictionary keys are strings and recursively sorts all dictionary keys alphabetically.
   - Rejects non-finite floats (`NaN`, `Infinity`, `-Infinity`) which violate RFC 8259 JSON specifications.
   - Distinguishes boolean and integer types (e.g. `True` vs `1`).
   - Generates compact, deterministic UTF-8 JSON representations with separators `(',', ':')`.

2. **Added Cryptographic Argument Digest Calculation**:
   Added `compute_argument_hash(args: Dict[str, Any]) -> str`:
   - Computes a deterministic 64-character hexadecimal SHA-256 digest over the canonical argument JSON.

3. **Cryptographic Binding in `IssuedConsent`**:
   - Replaced raw token storage with `token_hash: str` (SHA-256 of raw single-use token).
   - Added `argument_hash: str` to bind the token cryptographically to approved parameters.
   - Added `conversation_id: str` to bind the token strictly to the conversation turn.
   - Added `tier: PermissionTier` to bind the authorization level.
   - Converted `expires_at` to use `time.monotonic()` instead of wall-clock time, protecting against system clock tampering or NTP drift.

4. **Synchronized Atomic Verification & Consumption**:
   - Added `self._lock = threading.Lock()` to `PermissionBoundary`.
   - Updated `verify_consent_token(token, action_name, args, conversation_id)`:
     - Hashes the presented token and looks it up in `self._active_consents`.
     - Validates monotonic expiration, conversation ID, and action name.
     - Computes the SHA-256 digest of the incoming `args` and validates it against `consent.argument_hash`.
     - Atomically consumes and removes the token (`self._active_consents.pop(token_hash)`).

---

### 2.2 `runtime/tools/registry.py`
**Defect Addressed**: `FINDING-ADV-08` (L3 Consent Enforcement in Tool Dispatch)

#### Changes:
1. **Propagated Conversation Context & Parameter Verification**:
   - Updated `ToolRegistry.execute(tool_name, args, consent_token=None, conversation_id="")`.
   - In L3 permission validation:
     ```python
     if self.permission_boundary:
         if not consent_token:
             return {
                 "status": "ERROR",
                 "error": f"SECURITY GATE: {tool_name} denied - Action '{tool_name}' requires explicit user consent (Level L3_PROTECTED).",
                 "tool": tool_name,
                 "tier": "L3_PROTECTED"
             }
         if not self.permission_boundary.verify_consent_token(
             token=consent_token,
             action_name=tool_name,
             args=args,
             conversation_id=conversation_id,
         ):
             return {
                 "status": "ERROR",
                 "error": f"SECURITY GATE: {tool_name} denied - Invalid or unverified consent token presented.",
                 "tool": tool_name,
                 "tier": "L3_PROTECTED"
             }
     ```

---

### 2.3 `runtime/mcp/transport.py`
**Defect Addressed**: `FINDING-ADV-10` (Unbounded Line Buffers in MCP Stdio Transport)

#### Changes:
1. **Configured Maximum Buffer Ceilings**:
   - `MAX_MESSAGE_LINE_LENGTH_BYTES = 16 * 1024 * 1024` (16 MB cap per JSON-RPC frame).
   - `MAX_STDERR_LINE_LENGTH_BYTES = 64 * 1024` (64 KB cap per stderr line).

2. **Added Line Buffer Guards**:
   - In `stdout_reader`: Monitored accumulated stream buffer length. If accumulated bytes exceed 16 MB before encountering a newline delimiter, raises a `ValueError` into the reader stream and terminates cleanly to prevent out-of-memory exhaustion.
   - In `stderr_logger`: Truncates lines exceeding 64 KB and logs an explicit truncation warning to prevent logging memory bloat.

---

### 2.4 `runtime/mcp/client.py`
**Defect Addressed**: `FINDING-ADV-09` (Win32 Job Object Handle Leak & Concurrent Teardown Race)

#### Changes:
1. **Re-Entrant Synchronization Lock**:
   - Changed `self._lock = threading.Lock()` to `threading.RLock()` to prevent re-entrant deadlocks between `start()`, `stop()`, and `_terminate_subprocesses()`.

2. **Atomic Job Object Teardown**:
   - In `_terminate_subprocesses()`:
     ```python
     with self._lock:
         job = self._job
         self._job = None

     if job:
         try:
             job.terminate(exit_code=0)
         except Exception as e:
             logger.warning(f"Error terminating Job Object for '{self.config.server_id}': {e}")
         try:
             job.close()
         except Exception as e:
             logger.warning(f"Error closing Job Object for '{self.config.server_id}': {e}")
     ```
     This atomic extraction guarantees that only one thread can ever call `job.close()`, completely eliminating the `'NoneType' object has no attribute 'close'` race condition and preventing unclosed Job Object handles.

3. **Guaranteed Thread & Loop Cleanup in `stop()`**:
   - Updated `stop()` so that even if the session state had already transitioned to `STOPPED` (due to an early worker loop exit), `self._thread.join()` and `self._cleanup_loop()` are always executed, preventing leaked OS thread and IOCP event loop handles.

4. **Fail-Closed Teardown in `start()`**:
   - If startup times out or fails initialization, `self.stop()` is invoked immediately to release all thread, loop, and process handles.

---

## 3. Test Suites Created

Six new adversarial test suites were created in `tests/adversarial/` (19 new tests, bringing repository adversarial test total to 49 tests):

1. **`tests/adversarial/test_adv_consent_integrity.py` (8 tests)**:
   - Validates argument value substitution rejection, nested argument mutation rejection, type confusion rejection (`True` vs `1`), `NaN`/`Infinity` rejection, conversation mismatch rejection, token replay rejection, token expiration rejection, and atomic concurrent token consumption.
2. **`tests/adversarial/test_adv_nested_job_objects.py` (4 tests)**:
   - Empirically verifies child process containment within pre-existing outer Job Objects (Scenarios A through E), dual job membership, and hierarchy teardown on parent death.
3. **`tests/adversarial/test_adv_transport_eof.py` (2 tests)**:
   - Verifies handling of premature server EOF before initialization and abrupt server suicide mid-tool execution.
4. **`tests/adversarial/test_adv_transport_cancellation.py` (2 tests)**:
   - Verifies tool call timeout cancellation and clean session stop during in-flight tool execution.
5. **`tests/adversarial/test_adv_transport_backpressure.py` (2 tests)**:
   - Verifies high-frequency sequential tool bursts (30 calls) and 1 MB message framing integrity.
6. **`tests/adversarial/test_adv_lifecycle_matrix.py` (1 test: 195 lifecycles)**:
   - Verifies system resource stability across 100 normal cycles, 25 failed startups, 50 artifact cycles, and 20 multi-session cycles (60 parallel sessions).

---

## 4. Verification Summary

- **Adversarial Test Suite (`tests/adversarial/`)**: 49 passed, 0 failures, 0 errors in 83.74s.
- **Baseline Regression Suite (`tests/run_all.py`)**: 105 passed, 2 skipped, 0 failures, 0 errors in 17.52s.
- **Total Discovered Tests**: 156 tests (154 passed, 2 skipped).
- **Resource Stability**: Zero handle leaks (< 0.08 handles/cycle across 195 cycles), zero worker thread leaks, zero orphan processes.
