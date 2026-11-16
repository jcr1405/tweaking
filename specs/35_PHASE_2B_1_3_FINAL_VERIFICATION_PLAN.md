# Specification 35: Phase 2B.1.3 Final Verification Plan

## 1. Mission & Context

This document establishes the comprehensive test plan, threat models, architectural hypotheses, and empirical verification methodologies for **Presence Backend Phase 2B.1.3 (Final Security, Transport & Resource Verification Closure)**.

Phase 2B.1.2 demonstrated foundational remediations for argument hashing, nested Job Object containment, buffer ceilings, and lifecycle synchronization. However, several critical security and systems engineering hypotheses require deeper adversarial demonstration:

1. **Permission-Tier Binding Enforcement**: Storing an authority tier in `IssuedConsent` is insufficient unless the execution pipeline actively verifies that the stored tier strictly matches the authoritative local `ToolDefinition` tier.
2. **Authorization TOCTOU & Mutable Reference Elimination**: Verifying an argument hash before execution does not prevent an authorization Time-Of-Check to Time-Of-Use (TOCTOU) vulnerability if the caller or concurrent thread mutates the underlying dictionary prior to handler dispatch.
3. **Consent Issuance Authority Gate**: Verifying that untrusted entities (such as model responses or external MCP servers) cannot forge, auto-grant, or bypass explicit local user consent.
4. **Descendant Job Object Breakaway Defense**: Emitting real Win32 `CREATE_BREAKAWAY_FROM_JOB` (0x01000000) requests across a multi-tier descendant process tree (parent -> MCP -> child -> grandchild) to empirically verify Windows 11 kernel containment semantics.
5. **Slow-Consumer Transport Backpressure**: Demonstrating genuine flow control when a fast MCP producer sends data faster than a delayed consumer reads it, proving that AnyIO rendezvous memory streams and Windows kernel pipe buffers prevent unbounded memory accumulation.
6. **Exact 16 MB JSON-RPC Boundary Verification**: Rigorously testing line boundaries at 16 MB - 1 byte, exactly 16 MB, and 16 MB + 1 byte, clarifying character vs byte semantics and fail-closed termination.
7. **Complete 7-Workload Lifecycle Matrix**: Testing all 7 distinct lifecycle categories separately without combining unlike metrics:
   - 100 Normal MCP server cycles
   - 25 Abrupt parent-process terminations (via external monitor process)
   - 25 Failed MCP startups and cleanups
   - 25 MCP crash and bounded restart cycles
   - 50 Large artifact creations and retrievals
   - 20 Groups of 3 concurrent MCP sessions (60 sessions)
   - 25 Full Presence runtime startup and shutdown cycles
8. **Resource Telemetry & Plateau Investigation**: Measuring handle and thread delta across repeated batches to prove whether resource consumption plateaus or leaks.
9. **Live Gemini Integration Gate**: Transparent reporting on live API verification; if credentials are absent, documenting as NOT RUN without synthetic mocks.

---

## 2. Governing Invariants

The following architectural invariants remain non-negotiable throughout this milestone:
1. **Python 3.12 Runtime**: Official MCP Python SDK 2.3.0 owns protocol handling.
2. **Win32 Process Containment**: Native Windows APIs own process creation (`CREATE_SUSPENDED`), containment (`Win32JobObject`), handle isolation (`PROC_THREAD_ATTRIBUTE_HANDLE_LIST`), and thread resumption (`ResumeThread`).
3. **Presence Authorization**: `PermissionBoundary` and `ToolRegistry` own authorization, execution, persistence, and recovery.
4. **Zero Out-of-Scope Dependencies**: No Rust, no Rust sidecars, no replacing MCP SDK, no rewriting `AgentExecutor`, no second permission system.

---

## 3. Investigation & Verification Methodologies

### 3.1 Part 3: L3 Consent Authorization Integrity & TOCTOU Protection

#### Threat Model:
- **T-01: Tier Confusion**: An attacker or model obtains a consent token for an L0 observation action and passes it to authorize an L3 protected operation (e.g. disk deletion).
- **T-02: TOCTOU Argument Mutation**: Caller submits mutable dictionary `{"path": "safe.txt"}`, token is verified, then caller mutates `args["path"] = "C:\\Windows\\System32\\..."` before the tool handler executes.
- **T-03: Model Auto-Consent**: Model crafts a tool call containing `granted_consent: True` or attempts to invoke internal consent issuance APIs.
- **T-04: Tool Definition Mutation**: A tool's registered tier is altered between token issuance and execution.

#### Verification Strategy:
1. **Tier Comparison**: `verify_consent_token` must accept `expected_tier: AuthorityLevel`. It must strictly check that `consent.tier == expected_tier` and `current_policy.required_level == expected_tier`. Missing or invalid tiers must fail closed.
2. **Immutable Argument Snapshot**: `ToolRegistry.execute()` will canonicalize incoming arguments to JSON and reconstruct an independent, isolated snapshot (`json.loads(canonical_json)`) before verification. The tool handler will be invoked exclusively with this verified snapshot, rendering concurrent mutations to the original dictionary ineffective.
3. **Canonicalization Rigor**: Test key ordering, nested dictionaries, nested lists, non-string keys, `NaN`/`Infinity`, negative zero normalization, boolean vs integer distinction, and custom objects.
4. **Authority Path Audit**: Enforce that `AgentExecutor` passes no consent token, ensuring model requests for L3 tools consistently hit the security gate.

---

### 3.2 Part 4: Real Descendant Breakaway Verification

#### Threat Model:
- **T-05: Descendant Breakaway**: An adversarial MCP subprocess attempts to escape process tree containment by spawning child or grandchild processes with `CREATE_BREAKAWAY_FROM_JOB` (0x01000000).

#### Test Fixture: `tests/fixtures/job_breakaway_fixture.py`
- Spawns as child of MCP server.
- Uses native `CreateProcessW` with `CREATE_BREAKAWAY_FROM_JOB`.
- Queries `IsProcessInJob` against outer and inner Job Objects.
- Records exact Win32 error codes (e.g. `ERROR_ACCESS_DENIED` = 5).
- Tests Scenarios A through E:
  - **Scenario A**: Outer Job Object without breakaway permission.
  - **Scenario B**: Outer Job Object with `JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK`.
  - **Scenario C**: Outer Job Object explicitly permitting breakaway.
  - **Scenario D**: Inner Job Object refusing breakaway.
  - **Scenario E**: Outer Job Object destruction (verifying complete tree termination).

---

### 3.3 Part 5: Transport Backpressure & 16 MB Line Boundaries

#### Threat Model:
- **T-06: Fast Producer / Slow Consumer Memory Bloat**: Malicious or runaway MCP server emits high-throughput responses faster than Presence consumes them.
- **T-07: Buffer Exhaustion**: Subprocess emits lines exceeding 16 MB or omitting newlines.

#### Verification Strategy:
1. **Backpressure Demonstration**: Build a dedicated fixture server that attempts to write 1 MB, 10 MB, and 50 MB to stdout as fast as possible, while the test consumer introduces simulated processing delays.
2. **Rendezvous Channel Analysis**: Measure whether AnyIO's zero-capacity memory stream blocks the reader, filling the Windows kernel pipe buffer and blocking the child's `WriteFile` call.
3. **Exact 16 MB Boundary Test**:
   - 16 MB - 1 byte payload -> Accepted if valid JSON-RPC.
   - Exactly 16 MB payload -> Strictly defined and tested.
   - 16 MB + 1 byte payload -> Immediately rejected with fail-closed stream termination.
   - Non-delimited streams exceeding 16 MB -> Bounded memory guard triggers clean failure.

---

### 3.4 Part 6: Transport Cancellation and EOF

#### Threat Model:
- **T-08: Transport Deadlock on Abrupt EOF or Cancellation**: MCP subprocess crashes mid-write or consumer cancels while writer is blocked.

#### Verification Strategy:
- Inject cancellation and EOF across stdout reads, stdin writes, memory stream sends, tool execution, and shutdown.
- Verify pending futures unblock immediately, state transitions to `FAILED`, and worker threads terminate.

---

### 3.5 Part 7: Complete 7-Workload Lifecycle Matrix

#### Distinct Workloads Tested Separately:
1. **Normal Server Lifecycles** (100 repetitions): Full start -> tool execution -> clean shutdown.
2. **Abrupt Parent Termination** (25 repetitions): External controller spawns parent and contained MCP tree, kills parent with `taskkill /F`, and independently verifies all descendant PIDs are dead.
3. **Failed Startup Lifecycles** (25 repetitions): Invalid executables, invalid cwd, injection failures.
4. **MCP Crash and Restart** (25 repetitions): Immediate crash, crash during execution, verify non-idempotent mutations are never replayed.
5. **Large Artifact Operations** (50 repetitions): 50 KB text payloads stored and retrieved from disk.
6. **Concurrent MCP Sessions** (20 groups x 3 sessions = 60 sessions): Parallel sessions running simultaneously.
7. **Full Runtime Lifecycles** (25 repetitions): Full Presence stack initialization and shutdown.

#### Telemetry & Plateau Investigation:
- Measure handles, threads, working set, and private bytes before and after each workload group.
- Execute repeated batches to establish that resource consumption plateaus rather than accumulating monotonically.

---

### 3.6 Part 8: Live Gemini Integration Gate

- Check environment for `GEMINI_API_KEY`.
- If absent, record gate as **NOT RUN** with detailed instructions for the operator.
- If present, execute live end-to-end turn through the hardened Win32 transport and verify `thought_signature` continuation.

---

## 4. Final Acceptance Gates Matrix

| Gate | Category | Acceptance Criteria |
| :--- | :--- | :--- |
| **Gate A** | Consent Integrity | Stored tier strictly matches tool tier; immutable argument snapshot prevents TOCTOU; model cannot issue consent. |
| **Gate B** | Windows Containment | Real `CREATE_BREAKAWAY_FROM_JOB` tested; all descendants contained; parent death reaps tree. |
| **Gate C** | Transport | Slow consumer backpressure demonstrated; 16 MB boundary strictly enforced; zero task leaks. |
| **Gate D** | Lifecycle & Resources | All 7 distinct workload categories executed and reported separately; resource plateau empirically proven. |
| **Gate E** | Live Gemini | Live turn verified if key present; transparently reported as NOT RUN if absent. |
| **Gate F** | Regression | All existing baseline (105 passed, 2 skipped) and adversarial tests pass without regression. |
