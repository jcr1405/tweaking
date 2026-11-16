# SPECIFICATION 29: PHASE 2B.1.1 ADVERSARIAL VERIFICATION PLAN

**Status**: ACTIVE AUDIT PLAN  
**Date**: 2026-11-16  
**Milestone**: Phase 2B.1.1 — Adversarial Reliability, Process Containment & Security Verification  
**Branch**: `verification/phase-2b-1-adversarial`  
**Governing Invariant**: *Python remains the Presence application runtime. The official MCP Python SDK owns protocol communication. Native Windows APIs own process creation and containment. Presence owns tool authorization, execution, persistence, and recovery.*

---

## 1. Executive Mission & Verification Philosophy

Ordinary automated unit tests and integration demonstrations establish that a system functions when inputs are valid and the environment behaves normally. They do not prove that the system remains safe and reliable when:
- Child processes crash, freeze, or spawn malicious process trees.
- Transports experience byte fragmentation, bursts, or unexpected closures.
- Parent processes terminate abruptly.
- Environment variables attempt injection or ambient secret leakage.
- Adversarial MCP servers attempt Job Object breakaway or prompt injection.
- High-concurrency operations race on SQLite, artifacts, or async streams.
- Repeated lifecycles trigger handle, thread, or memory leaks.

**Core Verification Objective**:
> Actively attempt to break the Presence Runtime. Test against real Windows operating system behaviour, failure injection, repeated stress lifecycles, and adversarial attacks. Uncover hidden defects without modifying production source code during Stage A.

---

## 2. Threat Model & Audit Scope

```
                ┌────────────────────────────────────────────────────────┐
                │             Untrusted Environment & Inputs             │
                │  - Malicious MCP Tool Descriptions (Prompt Injection)  │
                │  - Ambient Secrets (GEMINI_API_KEY, ANTHROPIC_API_KEY) │
                │  - Dangerous Env Overrides (APPINIT_DLLS, PATHEXT)     │
                │  - Adversarial MCP Processes (Grandchildren, Escapes)  │
                └──────────────────────────┬─────────────────────────────┘
                                           │
                                           ▼
┌────────────────────────────────────────────────────────────────────────┐
│                        PRESENCE SECURITY SHIELD                        │
│                                                                        │
│  [1. Policy A & B]       [2. Win32 Suspended Launcher]                 │
│  - Strip secrets         - CREATE_SUSPENDED launch                     │
│  - Denylist 31 overrides - PROC_THREAD_ATTRIBUTE_HANDLE_LIST (3 pipes) │
│  - Whitelist Win32 env   - AssignProcessToJobObject BEFORE Resume      │
│                          - IsProcessInJob verification                 │
│                                                                        │
│  [3. Transport Adapter]  [4. Permission Boundary & Default-Deny]       │
│  - win32_mcp_stdio_      - Unapproved tools omitted from registry      │
│    transport             - Never declared to Gemini                    │
│  - AnyIO memory streams  - Hardened authority tiers (L0 - L3)          │
│  - Zero monkey-patching  - Mandatory explicit consent for L3           │
└────────────────────────────────────────────────────────────────────────┘
```

The audit evaluates 14 critical test areas (A through N):
1. **Area A: Win32 Suspended Process Launcher**: Ctypes definitions, 64-bit alignment, execution order, error-path cleanup, executable resolution, Unicode environment blocks.
2. **Area B: Job Object and Process-Tree Containment**: Child/grandchild inheritance, `CREATE_BREAKAWAY_FROM_JOB` behavior, abrupt parent termination, handle ownership, nested Job Objects.
3. **Area C: Handle Inheritance Security**: `PROC_THREAD_ATTRIBUTE_HANDLE_LIST`, adversarial parent handles (files, pipes, events, mutexes), child handle leak verification.
4. **Area D: MCP Stdio Transport Reliability**: Byte-by-byte fragmentation, multiple JSON-RPC messages per read, backpressure, stderr/stdout separation, cancellation, stream cleanup.
5. **Area E: MCP Server Lifecycle & Supervisor**: Startup failures, mid-call crashes, exponential backoff, concurrent multi-server operations, shutdown.
6. **Area F: Environment Security**: Secret isolation, Windows runtime integrity, server override rejection.
7. **Area G: Tool Approval & Permission Boundary**: Default-deny enforcement, tier classification, prompt injection immunity, L3 consent gating.
8. **Area H: Artifact Storage & Large Outputs**: Exact boundary sizes (40959/40960/40961 bytes), chunked retrieval, concurrent 200 MB quota enforcement.
9. **Area I: Persistence and Crash Consistency**: SQLite WAL canonicality, JSONL reconciliation, exactly-once vs at-least-once recovery.
10. **Area J: Gemini Live Integration**: Live turn through new suspended transport, trajectory recording.
11. **Area K: Repeated Lifecycle & Resource Leaks**: 100x server cycles, 25x abrupt terminations, 50x artifact writes; telemetry on handles, memory, threads, tasks.
12. **Area L: Concurrency & Race Conditions**: Overlapping tool calls, simultaneous cancellations, shutdown during pending I/O.
13. **Area M: Shutdown & Recovery**: Idempotent shutdown, state preservation across restarts.
14. **Area N: Independent Architectural Review**: Critical challenge of core assumptions.

---

## 3. Audit Protocol & Execution Rules

### Stage A — Read-Only Adversarial Verification Rules
- Production source files (`runtime/`) MUST NOT be modified.
- All adversarial fixtures and test harnesses must reside in `tests/adversarial/`.
- Fault injection must use isolated wrappers; no global patching that affects unrelated subsystems.
- Every discovered defect or vulnerability must be documented with stable ID, severity, reproduction, and evidence.

### Stage B — Controlled Remediation Rules
- Fix only demonstrated defects or high-confidence vulnerabilities.
- For each fix, create a failing regression test proving the defect before applying the fix.
- Verify that all 107 baseline tests and new adversarial tests pass.
- Maintain commit checkpoints on `verification/phase-2b-1-adversarial`.

---

## 4. Acceptance Criteria & Gates

| Gate | Verification Target | Pass Criteria |
| :--- | :--- | :--- |
| **Gate A: Containment** | Suspended launch, Job containment, Breakaway | 0 processes escape; abrupt parent death terminates descendants |
| **Gate B: Transport** | Fragmentation, Backpressure, Cancellation | No hung tasks, clean JSON-RPC parsing, no memory explosion |
| **Gate C: Security** | Handle isolation, Secrets, Default-deny | Only 3 pipes inherited; 0 secrets leaked; unapproved tools blocked |
| **Gate D: Persistence** | SQLite WAL, Chunked artifacts, Quotas | No corrupt state, exactly 40 KB boundary respected, quota honored |
| **Gate E: Stability** | Repeated lifecycles (100+ cycles) | No handle leaks, no thread accumulation, memory returns to baseline |
