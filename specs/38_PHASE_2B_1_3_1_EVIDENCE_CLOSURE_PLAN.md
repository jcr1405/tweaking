# PRESENCE BACKEND SPECIFICATION 38
# Phase 2B.1.3.1 — Final Evidence Reconciliation & Verification Closure Plan

## 1. Executive Mission & Background

Phase 2B.1.3 reported substantial security and reliability improvements across process containment, transport message boundaries, and authorization integrity. However, a forensic review identified discrepancies between the approved verification plan and reported evidence.

The mission of Phase 2B.1.3.1 is to conduct an evidence-closure operation on the Presence Runtime repository (`c:\Users\chait\OneDrive\Documents\projects\tweaking`):
- Reconcile test accounting discrepancies to ensure 100% mathematical consistency.
- Implement and execute the exact Windows Job Object Scenarios A through E as defined in Specification 35.
- Implement end-to-end Windows pipe backpressure testing with a real subprocess across 1 MB, 10 MB, and 50 MB with slow consumers.
- Audit the RFC 8785 canonicalization claim and accurately categorize guarantees and boundaries.
- Complete the full lifecycle matrix, specifically adding invalid working directories and the critical non-idempotent no-replay verification with observable side effects.
- Standardize resource telemetry with explicit process identity, working set, private bytes, and plateau proof.
- Re-verify all baseline and adversarial suites without weakening acceptance criteria.

---

## 2. Initial Discrepancy Register

| Finding ID | Subsystem | Previous Reported State | Discrepancy Identified | Planned Remediation |
|---|---|---|---|---|
| **DISCREPANCY-01** | Test Accounting | 83 adversarial tests reported in summary; module list summed to 93 | Unittest runner discovered exactly 83 tests. Section 2.3 contained manual transcription errors in 7 modules | Reconcile every module; generate machine-readable `test_inventory.json`; verify total is exactly 190 (107 baseline + 83 adversarial) |
| **DISCREPANCY-02** | Windows Job Breakaway | Scenarios A–E reported in Spec 36 | `test_adv_nested_job_objects.py` had duplicate scenario names; Scenario B tested `attempt_breakaway` rather than `SILENT_BREAKAWAY_OK` auto-exclusion; Scenario E lacked a full 3-tier tree | Enhance `job_breakaway_fixture.py` and test suite to execute the exact original A–E matrix from Spec 35 |
| **DISCREPANCY-03** | Transport Backpressure | AnyIO zero-capacity memory stream tested | Only application-level memory stream was tested; did not demonstrate subprocess pipe-buffer saturation | Build a real subprocess writing 1 MB, 10 MB, and 50 MB frames to stdout; measure consumer delays, process memory, and pipe backpressure |
| **DISCREPANCY-04** | Canonicalization Claim | "100% RFC 8785 Compliance" claimed | Implementation uses Python `json.dumps(sort_keys=True)`: Unicode code point sort vs UTF-16 surrogate units; Python float format | Accurately rename to "Strict JSON Argument Normalization and Deterministic Python Serialization"; document exact guarantees vs JCS |
| **DISCREPANCY-05** | Lifecycle Matrix | 7 workloads reported | Workload 3 only tested non-existent file; Workload 4 lacked empirical proof of non-idempotent no-replay with observable side effects | Expand Workload 3 to test invalid cwd and launch failures; implement controlled side-effect tool test verifying no blind replay on crash |
| **DISCREPANCY-06** | Resource Telemetry | Baseline at 241 and 245-251 handles | Measurement scope and process identity were not explicitly documented; private bytes omitted | Document exact PID, process name, working set, private bytes, and repeated plateau delta |

---

## 3. Repository Baseline & Git Protection

- **Repository Path**: `C:\Users\chait\OneDrive\Documents\projects\tweaking`
- **Starting Baseline Branch**: `verification/phase-2b-1-3-final-closure`
- **Starting Baseline Commit**: `29fdcd1` ("feat(phase-2b-1-3): final security, transport and resource verification closure")
- **Dedicated Closure Branch**: `verification/phase-2b-1-3-1-evidence-closure`
- **Governing Constraints**:
  - Python 3.12, official MCP Python SDK 2.3.0, native Win32 APIs.
  - Zero Rust, zero Rust sidecars, no new external runtime dependencies.
  - No changes to conversation persistence schemas or `AgentExecutor` turn loop.

---

## 4. Verification Methodology & Test Matrix

### 4.1 Test Count Reconciliation
1. Execute `tests/run_all.py` (baseline) and capture raw test results, failures, errors, skips.
2. Execute `unittest discover -s tests/adversarial -p "test_adv_*.py"` and capture raw test results.
3. Generate `specs/test_inventory.json` with machine-readable counts per module and combined totals.

### 4.2 Windows Job Object Scenarios A–E Matrix (Specification 35)
- **Scenario A**: Outer Job Object without breakaway permission. Child attempts `CREATE_BREAKAWAY_FROM_JOB` (0x01000000). Verify child creation fails with Win32 Error 5 (`ERROR_ACCESS_DENIED`).
- **Scenario B**: Outer Job Object with `JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK`. Child created *without* breakaway flag. Verify child is automatically excluded from outer job.
- **Scenario C**: Outer Job Object with `JOB_OBJECT_LIMIT_BREAKAWAY_OK`. Child created with `CREATE_BREAKAWAY_FROM_JOB`. Verify child escapes outer job; if inner job without breakaway exists, child is trapped in inner job.
- **Scenario D**: Outer Job permits breakaway; Inner Job refuses breakaway. Process inside inner job attempts breakaway. Verify child creation fails or child remains trapped in inner job.
- **Scenario E**: Process tree: `controller -> MCP parent -> child -> grandchild`. Assigned to Job hierarchy. Outer job terminated/closed with `KILL_ON_JOB_CLOSE`. External controller verifies all 3 PIDs terminated with zero survivors.

### 4.3 End-to-End Transport Pipe Backpressure
- Fixture: Native subprocess writing newline-delimited JSON-RPC frames over Windows pipe.
- Payloads: 1 MB, 10 MB, and 50 MB total volume.
- Consumer configurations:
  1. Baseline (unthrottled).
  2. Slow consumer (1 ms per frame delay).
  3. Slow consumer (10 ms per frame delay).
- Measurements:
  - Total frames sent vs received.
  - Producer write duration vs consumer read duration.
  - Presence process working set & private bytes (must remain flat and bounded).
  - Subprocess write blocking / elapsed time.
  - Cancellation: mid-stream cancellation unblocks reader and terminates subprocess without orphan leaks.

### 4.4 Canonicalization Audit & Qualification
- Compare `canonicalize_arguments()` against RFC 8785 requirements.
- Evaluate Unicode surrogate pairs (U+10000+), floating point formatting, and key sorting.
- Adopt Outcome B: Reclassify claim as **Strict JSON Argument Normalization and Deterministic Python Serialization**.
- Document exact guarantees and boundaries.

### 4.5 Lifecycle Matrix Completion & Non-Idempotent No-Replay Check
- Workload 3 additions: Invalid working directory, missing binary, invalid startup parameters, injection failure at assignment.
- Workload 4 non-idempotent test:
  1. Durable side-effect tool writes unique operation ID to local file.
  2. Tool process crashes immediately via `os._exit(1)`.
  3. Supervisor detects crash and restarts server to restore availability.
  4. Verify that supervisor returns error status and **does not replay** the tool call.
  5. Check file: exactly 1 side effect occurred, never 2.

### 4.6 Resource Telemetry Standardization
- Standardize measurements: PID, Process Name, Working Set (MB), Private Bytes (MB), Handle Count, Thread Count.
- Measure cold baseline, post-warmup, and repeated batches (15 cycles, 25 cycles, 50 cycles).

---

## 5. Acceptance Gates

| Gate | Requirement | Target Condition |
|---|---|---|
| **Gate A** | Test Accounting | Baseline, adversarial, per-module, and combined counts reconcile 100% |
| **Gate B** | Windows Containment | Original Scenarios A–E independently tested and accurately represented |
| **Gate C** | End-to-End Backpressure | Real subprocess pipe flow control and bounded memory demonstrated across 1 MB, 10 MB, 50 MB |
| **Gate D** | Canonicalization | Scope accurately classified as Strict JSON Normalization & Deterministic Serialization |
| **Gate E** | Lifecycle Recovery | Missing startup and in-flight crash cases tested; non-idempotent side effect is not replayed |
| **Gate F** | Resource Behavior | Repeated measurements use consistent process identity and demonstrate plateau (delta <= 2) |
| **Gate G** | Consent Security | L3 tier binding, authorized snapshot isolation, and trusted issuance verified |
| **Gate H** | Regression | Baseline (105 passed, 2 skipped) and full adversarial suites pass with zero regressions |
| **Gate I** | Live Gemini Reasoning | Explicitly documented as `NOT RUN (CREDENTIAL ABSENT)` without synthetic mocks |

Each gate will be assigned exactly one status: `PASS`, `PARTIAL`, `FAIL`, or `NOT RUN`.
