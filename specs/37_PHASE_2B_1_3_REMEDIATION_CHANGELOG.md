# PRESENCE BACKEND SPECIFICATION 37
# Phase 2B.1.3 — Remediation & Engineering Changelog

## 1. Overview

This document records the exact code changes, architectural fixes, new test fixtures, and verification suites implemented during **Phase 2B.1.3 (Final Security, Transport & Resource Verification Closure)**.

All modifications strictly uphold the governing architectural invariants:
- Python 3.12 application runtime.
- Official MCP Python SDK 2.3.0 for protocol communication.
- Native Win32 APIs for process creation and containment.
- Zero Rust, zero Rust sidecars, no new external runtime dependencies.
- No changes to conversation persistence schemas or AgentExecutor ownership.

---

## 2. Source Code Remediations

### 2.1 `runtime/permissions.py`
- **RFC 8785 Canonicalization Compliance**:
  - Implemented `_validate_and_normalize_canonical_data(obj)`:
    - Recursively traverses Python objects destined for canonical serialization.
    - Strictly enforces string-only dictionary keys; raises `TypeError` if non-string keys (e.g., int, float, bool, None) are present.
    - Normalizes float `-0.0` to positive `0.0` per RFC 8785 Section 3.2.2.3.
    - Rejects non-finite floating point values (`NaN`, `Infinity`, `-Infinity`) with `ValueError`.
  - Updated `canonicalize_arguments(args)` to invoke `_validate_and_normalize_canonical_data` prior to JSON serialization.
- **L3 Stored Tier Enforcement & Token Hardening (`FINDING-ADV-12`)**:
  - Added parameter `expected_tier: Optional[AuthorityLevel] = None` to `verify_consent_token()`.
  - Added policy presence check: if `action_name` is missing from `self._policies`, execution fails closed immediately.
  - Enforced that `current_policy.required_level == consent.tier`: if a tool's required authority level was upgraded/altered between token issuance and execution, the token is invalidated.
  - Enforced that if `expected_tier` is provided, `consent.tier == expected_tier and current_policy.required_level == expected_tier`.
  - Replaced equality operator with `hmac.compare_digest` for constant-time comparison of argument hashes, mitigating timing side-channel attacks.

### 2.2 `runtime/tools/registry.py`
- **TOCTOU Argument Mutation Elimination (`FINDING-ADV-11`)**:
  - Remediated vulnerability where mutable dictionary references passed into `execute()` could be modified by concurrent threads or external callers between consent verification and tool dispatch.
  - Implementation in `ToolRegistry.execute()`:
    1. Early in dispatch, canonicalizes arguments and creates an independent snapshot:
       ```python
       canonical_json = canonicalize_arguments(args)
       authorized_args = json.loads(canonical_json)
       ```
    2. Passes `authorized_args` and `expected_tier=tool.authority_tier` to `verify_consent_token()`.
    3. Passes `authorized_args` to `check_permission()`.
    4. Passes `authorized_args` directly to `tool.handler(authorized_args)`:
       ```python
       return tool.handler(authorized_args)
       ```
    5. The tool handler never receives the caller's mutable dictionary reference. Any external or asynchronous mutation has zero impact on tool execution.

### 2.3 `runtime/mcp/transport.py`
- **Byte-Accurate 16 MB Line Buffer Tracking**:
  - Updated `stdout_reader` loop:
    - Replaced character-length buffer tracking with byte-length tracking using `len(chunk.encode(encoding))`.
    - Tracks cumulative buffer size in UTF-8 bytes (`buffer_byte_count`) against `MAX_MESSAGE_LINE_LENGTH_BYTES = 16 * 1024 * 1024`.
    - Prevents character-to-byte expansion vulnerabilities where multibyte UTF-8 characters could cause memory allocation beyond the 16 MB boundary before triggering a character-count limit.
    - Immediately raises `ValueError` and terminates reader on un-delimited frame overflow.

---

## 3. Test Fixtures & Harness Enhancements

### 3.1 Native Windows Breakaway Fixture (`tests/fixtures/job_breakaway_fixture.py`)
- Created a native Windows standalone executable script using `ctypes.windll.kernel32`:
  - Directly invokes `CreateProcessW` with `CREATE_BREAKAWAY_FROM_JOB` (`0x01000000`).
  - Queries `IsProcessInJob(GetCurrentProcess(), outer_job)` and `IsProcessInJob(child_handle, inner_job)`.
  - Captures Win32 error codes (`GetLastError()`) when breakaway is rejected (e.g. Error 5 `ERROR_ACCESS_DENIED`).
  - Emits JSON-structured status lines to `stdout` for assertion by test runners.

### 3.2 Exact Message Boundary Test Suite (`tests/adversarial/test_adv_transport_message_limits.py`)
- Created dedicated test module with 6 test cases:
  1. `test_exact_16mb_boundary_minus_one`: Verifies 16,777,215 byte frame succeeds.
  2. `test_exact_16mb_boundary`: Verifies 16,777,216 byte frame succeeds.
  3. `test_exact_16mb_boundary_plus_one_fails`: Verifies 16,777,217 byte frame fails closed.
  4. `test_un_delimited_stream_overflow_fails_closed`: Verifies stream exceeding 16 MB without newline is terminated.
  5. `test_multibyte_utf8_frame_boundary_safety`: Verifies 3-byte and 4-byte UTF-8 sequences are accurately bounded.
  6. `test_stderr_flooding_handled_without_deadlock`: Verifies 2,000 continuous stderr lines are processed asynchronously without stalling stdout JSON-RPC.

### 3.3 Enhanced Adversarial Suites
- `test_adv_consent_integrity.py`:
  - Expanded to 20 comprehensive unit and integration tests covering TOCTOU mutation, thread concurrency, RFC 8785 edge cases, and dynamic tier changes.
- `test_adv_nested_job_objects.py`:
  - Upgraded to execute native fixture Scenarios A through E, testing real `CREATE_BREAKAWAY_FROM_JOB` behavior on Windows 11 Build 26100.
- `test_adv_lifecycle_matrix.py`:
  - Refactored into 8 dedicated test methods matching the 7 requested workload categories and Workload 8 plateau telemetry.
  - Explicitly closed subprocess pipes and waited on process handles to eliminate `ResourceWarning` noise.

---

## 4. Test Verification Summary

### Baseline Regression Results (`tests/run_all.py`)
- **Total Tests**: 107
- **Passed**: 105
- **Skipped**: 2 (`test_live_gemini_generation`, `test_live_gemini_mcp_invocation` due to absent `GEMINI_API_KEY`)
- **Failures**: 0
- **Errors**: 0
- **Working Set**: 96.01 MB

### Adversarial Suite Results (`tests/adversarial/`)
- **Total Tests**: 83 across 17 modules
- **Passed**: 83
- **Failures**: 0
- **Errors**: 0

### Combined Test Count
- **190 tests discovered** (188 passed, 2 skipped, 0 failures, 0 errors).

---

## 5. Artifact & Code Inventory

| File Path | Action | Description |
|---|---|---|
| `runtime/permissions.py` | Modified | RFC 8785 canonicalization, string keys, negative zero, stored tier check, constant-time compare |
| `runtime/tools/registry.py` | Modified | TOCTOU argument mutation elimination via authorized snapshot isolation |
| `runtime/mcp/transport.py` | Modified | Byte-accurate 16 MB line buffer length tracking |
| `tests/fixtures/job_breakaway_fixture.py` | Created | Native Win32 fixture attempting `CREATE_BREAKAWAY_FROM_JOB` |
| `tests/adversarial/test_adv_transport_message_limits.py` | Created | Exact 16 MB frame boundary, UTF-8 multibyte, stderr flood tests |
| `tests/adversarial/test_adv_consent_integrity.py` | Modified | Expanded to 20 tests for TOCTOU, tier binding, RFC 8785 |
| `tests/adversarial/test_adv_nested_job_objects.py` | Modified | Updated Scenarios A-E using native fixture |
| `tests/adversarial/test_adv_lifecycle_matrix.py` | Modified | Implemented all 7 distinct workloads + Workload 8 plateau |
| `tests/adversarial/test_adv_transport_backpressure.py` | Modified | AnyIO memory stream zero-capacity flow control verification |
| `tests/adversarial/test_adv_transport_cancellation.py` | Modified | Request and stream cancellation safety |
| `tests/adversarial/test_adv_transport_eof.py` | Modified | Unexpected and partial EOF handling |
| `specs/35_PHASE_2B_1_3_FINAL_VERIFICATION_PLAN.md` | Created | Comprehensive verification plan and acceptance criteria |
| `specs/36_PHASE_2B_1_3_FINAL_VERIFICATION_REPORT.md` | Created | Final verification and forensic report |
| `specs/37_PHASE_2B_1_3_REMEDIATION_CHANGELOG.md` | Created | Detailed engineering changelog and code diff documentation |
