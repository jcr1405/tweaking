# PRESENCE BACKEND SPECIFICATION 40
# Phase 2B.1.3.1 — Remediation & Engineering Changelog

## 1. Executive Summary

This changelog records the specific source modifications, test fixture additions, test enhancements, and evidence reconciliation actions performed during **Phase 2B.1.3.1 (Final Evidence Reconciliation & Verification Closure)**.

The objective was to resolve discrepancies identified between approved specifications and reported verification evidence, ensuring that every engineering claim is directly supported by reproducible code, tests, and telemetry.

---

## 2. Inventory of Changes by File

### 2.1 Test Fixtures

#### `tests/fixtures/job_breakaway_fixture.py` (Modified)
- **Reason**: Previous version only supported `attempt_breakaway` mode with `CREATE_BREAKAWAY_FROM_JOB`. It lacked support for testing automatic child exclusion under `JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK` without the breakaway flag, and lacked support for a full 3-tier process tree.
- **Changes**:
  1. Added mode `standard_spawn`: Invokes `CreateProcessW` with standard flags (`CREATE_NO_WINDOW`, without `CREATE_BREAKAWAY_FROM_JOB`) to verify that `SILENT_BREAKAWAY_OK` automatically excludes the child process from the outer job.
  2. Added mode `spawn_tree`: Fixture process (Tier 1) spawns a child process (Tier 2), which spawns a grandchild process (Tier 3), writing both descendant PIDs to stdout JSON for external controller verification during whole-tree termination.
  3. Added proper error logging to stderr when `OpenJobObjectW` or `IsProcessInJob` calls fail.

#### `tests/fixtures/pipe_backpressure_fixture.py` (Created)
- **Reason**: Required a genuine native Windows subprocess that writes continuous newline-delimited JSON-RPC frames over stdout to test Windows kernel pipe buffer saturation and flow control under slow consumers.
- **Features**:
  1. Accepts `target_bytes` and `frame_size_bytes` arguments.
  2. Emits valid newline-delimited JSON-RPC 2.0 notifications (`method: "notifications/stream"`).
  3. Uses binary unbuffered stdout (`sys.stdout.buffer`) with explicit `flush()`.
  4. Measures write duration per frame: flags a `pipe_blocked_event` if any single write/flush takes > 5 ms (indicating kernel pipe buffer saturation).
  5. Emits a final JSON-RPC notification (`method: "notifications/summary"`) with total bytes, frames, duration, and rate (MB/s).

### 2.2 Adversarial Test Suites

#### `tests/adversarial/test_adv_nested_job_objects.py` (Modified)
- **Reason**: Previous test suite had duplicate scenario names and did not accurately test the exact Scenarios A through E from Specification 35.
- **Changes**:
  1. Updated `test_scenario_a_outer_job_without_breakaway_denies_breakaway`: Tests child attempting `CREATE_BREAKAWAY_FROM_JOB` under default outer job; verifies that Windows 11 traps child in outer job (`child_in_outer == True`) or fails with Error 5 (`ERROR_ACCESS_DENIED`).
  2. Updated `test_scenario_b_outer_job_with_silent_breakaway_auto_excludes_child`: Tests standard spawn under `SILENT_BREAKAWAY_OK`; verifies parent is in outer job and child is automatically excluded.
  3. Updated `test_scenario_c_outer_job_permits_breakaway_with_explicit_flag`: Tests child with `CREATE_BREAKAWAY_FROM_JOB` under outer job with `BREAKAWAY_OK` and inner job without breakaway; verifies child breaks away from outer job but remains trapped in inner job.
  4. Updated `test_scenario_d_inner_job_refuses_breakaway`: Tests inner job refusal overriding outer job permissiveness.
  5. Updated `test_scenario_e_outer_job_destruction_terminates_entire_process_tree`: Tests full 3-tier tree (`Parent -> Child -> Grandchild`); verifies outer job termination kills all 3 processes with zero survivors.
  6. Renamed auxiliary tests clearly (`test_auxiliary_nested_assignment_failure_fails_closed`, `test_auxiliary_nested_parent_and_child_job_assignment`, `test_auxiliary_standalone_job_containment`).

#### `tests/adversarial/test_adv_transport_backpressure.py` (Modified)
- **Reason**: The previous suite only tested application-level AnyIO memory stream backpressure using simulated client ping-pongs. It did not test native Windows pipe buffers or streaming across 1 MB, 10 MB, and 50 MB volumes.
- **Changes**: Added 4 new test methods (+4 tests):
  1. `test_native_subprocess_pipe_backpressure_1mb`: Compares fast consumer vs slow consumer (1ms delay per frame); demonstrates 19 kernel pipe blocked events and significant throughput reduction while Presence memory growth remains < 5 MB.
  2. `test_native_subprocess_pipe_backpressure_10mb`: Streams 10 MB in ~10,000 JSON-RPC frames; asserts working set growth remains < 10 MB.
  3. `test_native_subprocess_pipe_backpressure_50mb`: Streams 50 MB in ~50,000 JSON-RPC frames; asserts flat memory curve (< 10 MB growth across 50 MB payload).
  4. `test_native_subprocess_midstream_cancellation_and_orphan_cleanup`: Cancels reading after 100 frames mid-stream; verifies reader unblocks, subprocess is terminated, and zero orphans survive.

#### `tests/adversarial/test_adv_lifecycle_matrix.py` (Modified)
- **Reason**: Workload 3 only tested missing script, and Workload 4 lacked empirical verification of non-idempotent operation safety on server crash.
- **Changes**:
  1. Expanded `test_workload_3_failed_mcp_startups`: Tests 25 cycles distributed across 4 distinct failure scenarios: missing executable, missing script, invalid working directory (`C:\non_existent_dir_presence_99999`), and immediate exit on boot.
  2. Added `test_workload_4b_non_idempotent_side_effect_no_blind_replay` (+1 test):
     - Tool writes unique operation ID to durable local file.
     - Server crashes via `os._exit(99)` before returning response.
     - Supervisor detects crash and restarts server to restore availability.
     - Supervisor returns error status (`"status": "ERROR"`, `"server_crashed": True`, `"NOT replayed"`).
     - Asserts local file contains **exactly 1 execution record** (no duplicate replay).
     - Asserts subsequent calls to the restarted server succeed cleanly.

### 2.3 Diagnostic & Accounting Infrastructure

#### `tests/generate_test_inventory.py` (Created)
- Python utility that programmatically inspects and counts tests in baseline and adversarial test suites, generating `specs/test_inventory.json`.

#### `tests/diagnose_resource_telemetry.py` (Created)
- Standalone diagnostic script executing Experiments A through D with native Win32 handle, thread, working set, and private byte metrics.

#### `specs/test_inventory.json` (Created)
- Machine-readable JSON inventory documenting the exact 107 baseline and 88 adversarial tests, skipped tests, module-level distribution, and combined totals (195 tests).

---

## 3. Discrepancies Resolved

| ID | Issue | Root Cause | Resolution |
|---|---|---|---|
| **EVIDENCE-01** | Test count reported as 83 in summary but summed to 93 in text | Manual transcription error in Spec 36 Section 2.3 text table | Reconciled all modules via programmatic discovery; generated `specs/test_inventory.json`; total verified at 88 adversarial + 107 baseline = 195 tests |
| **EVIDENCE-02** | Job Object scenarios did not match Spec 35 | Duplicate method names; Scenario B lacked silent auto-exclusion test; Scenario E lacked 3-tier tree | Enhanced `job_breakaway_fixture.py` with `standard_spawn` and `spawn_tree` modes; implemented exact Scenarios A–E in `test_adv_nested_job_objects.py` |
| **EVIDENCE-03** | AnyIO rendezvous test alone was not end-to-end backpressure | Tested only memory streams, not Windows pipe buffer saturation | Built `pipe_backpressure_fixture.py`; tested 1 MB, 10 MB, 50 MB streams; proved 19 kernel pipe blocks under slow consumer |
| **EVIDENCE-04** | RFC 8785 compliance claim scope was overbroad | Python `json.dumps` sorts code points (not UTF-16 code units) and uses C runtime float formatting | Reclassified claim as **Strict JSON Argument Normalization and Deterministic Python Serialization**; documented exact boundaries |
| **EVIDENCE-05** | Lifecycle matrix coverage gaps | Missing startup subcases and missing non-idempotent replay test | Expanded Workload 3 to test invalid cwd, missing binary; added Workload 4b side-effect replay test (verified exactly 1 write) |
| **EVIDENCE-06** | Resource telemetry baseline ambiguity | Process identity and measurement scope were not explicitly logged | Executed Experiments A–D measuring PID 31580; proved plateau delta = 0 handles, 0 threads across batches |

---

## 4. Test Suite Execution Summary

### Baseline Suite (`tests/run_all.py`):
- **Discovered**: 107 tests across 26 modules
- **Passed**: 105 tests
- **Skipped**: 2 tests (`test_gemini_live.py`, `test_mcp_live.py` due to absent `GEMINI_API_KEY`)
- **Failures / Errors**: 0
- **Idle Working Set**: 96.24 MB (Target < 100.0 MB)

### Adversarial Suite (`tests/adversarial/`):
- **Discovered**: 88 tests across 17 modules
- **Passed**: 88 tests
- **Skipped**: 0
- **Failures / Errors**: 0

### Combined Totals:
- **Discovered**: 195 tests
- **Passed**: 193 tests
- **Skipped**: 2 tests
- **Failures / Errors**: 0

---

## 5. Architectural Invariants Preserved

1. **Python 3.12 Runtime**: All application logic remains pure Python 3.12.
2. **Official MCP Python SDK 2.3.0**: Protocol handling remains 100% owned by the official SDK.
3. **Native Win32 APIs**: Process creation (`CreateProcessW`), handle isolation (`PROC_THREAD_ATTRIBUTE_HANDLE_LIST`), and containment (`Win32JobObject`) remain native Windows.
4. **No Rust**: Zero Rust dependencies, zero Rust sidecars introduced.
5. **No Schema Changes**: SQLite trajectory and JSONL event journal schemas remained unchanged.
6. **No AgentExecutor Rewrite**: Model reasoning loop and turn execution remained unmodified.
