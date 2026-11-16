# Specification 32: Phase 2B.1.2 Security Gap Closure & Final Adversarial Verification Plan

## 1. Context and Objective

During Phase 2B.1.1, seven defects were uncovered, reproduced with adversarial test fixtures, and remediated in `specs/30_PHASE_2B_1_ADVERSARIAL_VERIFICATION_REPORT.md` and `specs/31_PHASE_2B_1_REMEDIATION_CHANGELOG.md`.

However, an in-depth source inspection reveals several critical remaining gaps and incomplete verification requirements:

1. **L3 Consent-Token Argument Binding Vulnerability**: In `runtime/permissions.py`, `IssuedConsent` only records `context_keys = tuple(sorted(context.keys()))` and `verify_consent_token` only verifies `action_name` and expiry. The actual argument values are **not verified**, allowing an attacker or rogue model turn to substitute completely different parameters (e.g. substituting `path="C:\\sensitive\\data.db"` for an approved `path="C:\\temp\\file.txt"`).
2. **Nested Windows Job Object Compatibility**: While breakaway defense was demonstrated, execution inside an existing outer Windows Job Object (common in CI/CD, sandboxes, IDE runners, and containers) has not been empirically proven.
3. **MCP Stdio Transport Backpressure, Cancellation, EOF, and Message Size Limits**: Memory boundaries on un-delimited stdout streams, slow consumer backpressure, abrupt EOF propagation, and cancellation semantics require dedicated adversarial verification.
4. **Full Lifecycle Stress Matrix**: The full stress test matrix (100 normal cycles, 25 abrupt kills, 25 failed startups, 25 crashes, 50 large artifacts, 20 concurrent sessions, 25 runtime cycles) must be executed with operating-system telemetry.
5. **Live Gemini Integration**: Offline/online gate testing with explicit accounting for credential availability.

---

## 2. Threat Models & Test Architectures

### 2.1 Threat Model 1: L3 Consent Parameter Tampering & Replay Attacks
- **Attacker Profile**: Rogue model reasoning, prompt injection, or compromised subsystem.
- **Attack Vector A (Parameter Tampering)**: User explicitly approves an L3 operation for `tool_id="presence.delete_file"` with `args={"path": "C:\\temp\\log.txt"}`. The caller presents the issued consent token with `args={"path": "C:\\Windows\\System32\\driver.sys"}`. Because only the action name and token were verified, the destructive deletion executes on the sensitive target.
- **Attack Vector B (Type Confusion / Coercion)**: Changing parameter types (e.g., boolean `true` vs string `"true"`, integer `10` vs `10.0`, array modifications) to bypass application logic while preserving key names.
- **Attack Vector C (Conversation / Context Confusion)**: Using a token approved in conversation A to authorize an action in conversation B.
- **Attack Vector D (Concurrent Replay / TOCTOU)**: Firing two simultaneous execution requests using the same token before the token is consumed.
- **Defense Requirement**:
  - Deterministic canonical serialization of all arguments (canonical JSON with sorted keys, deterministic separators, rejection of non-JSON primitives like NaN/Infinity).
  - SHA-256 binding of `(action_name, argument_hash, conversation_id, permission_tier)`.
  - Atomic verification and consumption protected by thread locking.
  - In-memory storage of token hashes rather than raw secret tokens.
  - Monotonic clock (`time.monotonic()`) for tamper-resistant expiration comparisons.

### 2.2 Threat Model 2: Windows Job Object Nesting & Breakaway
- **Target OS**: Windows 11 (OS Build 26100+), Windows Server 2016+.
- **Scenarios**:
  - **Scenario A**: Normal execution without parent job.
  - **Scenario B**: Outer parent Job Object permits nested jobs (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` configured on outer job). Presence creates dedicated child Job Object and assigns suspended MCP subprocess.
  - **Scenario C**: Outer parent job prohibits nesting or restricts quota. Child assignment fails. Presence launcher must fail closed: terminate suspended child, close handles, and raise `RuntimeError`.
  - **Scenario D**: Breakaway attempt from nested job. Grandchild fails to escape containment.
  - **Scenario E**: Abrupt termination of the outermost parent Job Object. Entire hierarchy (parent, Presence, MCP child, and all descendants) terminated instantly by Windows kernel.

### 2.3 Threat Model 3: MCP Stdio Transport Exhaustion, Deadlock & Corruption
- **Vulnerabilities**:
  - **Unbounded Buffer Growth**: Stdio stream without newline delimiter could accumulate indefinitely if not bounded.
  - **Backpressure Deadlock**: AnyIO rendezvous channels (capacity 0) blocking writer or reader indefinitely during slow consumer.
  - **Abrupt EOF / Process Disconnect**: Broken pipe or abrupt child exit mid-stream must propagate EOF to `ClientSession` and unblock pending futures within bounded timeout.
  - **Cancellation Latency**: Cancelling a pending tool call must cleanly cancel the asyncio task and AnyIO stream without leaving zombie worker threads.
  - **Stderr Flood**: Millions of characters or rapid bursts on stderr must be throttled or bounded without polluting JSON-RPC stdout framing.

---

## 3. Adversarial Test Suites to Author in `tests/adversarial/`

| Test Suite File | Coverage Target | Key Test Cases |
| :--- | :--- | :--- |
| `tests/adversarial/test_adv_consent_integrity.py` | L3 Consent Binding & Integrity | Parameter tampering, type confusion, nested argument mutation, array alteration, conversation mismatch, replay attacks, concurrent replay, token expiry, token hash storage. |
| `tests/adversarial/test_adv_nested_job_objects.py` | Nested Win32 Job Objects | Scenarios A, B, C, D, E using real native Windows processes. |
| `tests/adversarial/test_adv_transport_backpressure.py` | Transport Backpressure & Bounds | Fast producer / slow consumer, large payload bursts, max line length bounds (16 MB cap), queue saturation. |
| `tests/adversarial/test_adv_transport_cancellation.py` | Transport Cancellation | Cancellation during read, write, tool execution; verification that cancellation unblocks caller. |
| `tests/adversarial/test_adv_transport_eof.py` | Abrupt EOF & Process Severance | Child exit during init, during tool listing, during tool execution, during large response streaming. |
| `tests/adversarial/test_adv_lifecycle_matrix.py` | Full Repeated-Lifecycle Matrix | 100 normal cycles, 25 parent kills, 25 failed starts, 25 crash/restarts, 50 large artifacts, 20 concurrent sessions, 25 runtime cycles with OS handle/memory telemetry. |

---

## 4. Execution Methodology

1. **Stage A (Read-Only Verification)**:
   - Author test suites.
   - Run tests against existing codebase.
   - Record exact failures (demonstrating reproduction of the consent argument substitution flaw and any transport boundaries).
2. **Stage B (Targeted Remediation)**:
   - Implement canonical argument hashing and atomic token verification in `runtime/permissions.py` and `runtime/tools/registry.py`.
   - Add maximum buffer size limits (16 MB) in `runtime/mcp/transport.py`.
   - Re-run all adversarial tests until 100% pass.
   - Re-run baseline regression suite (`tests/run_all.py`, 107 tests) to ensure zero regressions.
3. **Stage C (Reporting & Documentation)**:
   - Author `specs/33_PHASE_2B_1_2_ADVERSARIAL_VERIFICATION_REPORT.md`.
   - Author `specs/34_PHASE_2B_1_2_REMEDIATION_CHANGELOG.md`.
   - Record Git commit checkpoint.
