# Specification 56 — Phase 2C Sticky Fault-State Semantics, Proactor Debug Safety & Process Failure Closure

**Milestone:** Phase 2C Architecture Correction 11 (Final Architecture Freeze)  
**Author:** Principal Windows Systems Architect, Win32 Concurrency Specialist, Python Runtime Engineer & Application Security Reviewer  
**Status:** Approved & Frozen (Design Freeze Complete)  
**Repository Baseline:** `9726d9e` (`docs(phase-2c): close callback failure and identity guarantees`)  
**Scope:** Sticky Fault-State Lifecycle, Honest Process-Level Failure Taxonomy, Proactor Debug Configuration Safety, Join-Timeout Escalation Contract, Lifetime-Bounded Saturating Telemetry, 24 Concurrency Proofs, and Implementation Freeze Determination  
**Authoritative Predecessors:** Specifications 41 through 55  

---

## 1. Executive Summary

Specification 56 establishes the final architectural design freeze for Presence Phase 2C. It resolves the remaining operational edge cases and closes all open questions regarding fault monotonicity, process-level failure semantics, Proactor debug logging safety, and lifetime memory bounds:

1. **P2C-62 (Sticky Fault-State Semantics & Reinitialization Contract)**:
   Prohibited clearing `_faulted` inside `drain_and_reset()`. The faulted state is established as a **monotonic one-way latch** ($False \to True$) for the queue instance. Ordinary batch drains may clear entries and reconcile `_notification_pending = False`, but `_faulted` remains `True` permanently. Reinitialization requires instantiating a fresh queue and source after old hook and consumer tasks are fully terminated.
2. **P2C-63 (Honest Process-Level Failure Semantics & Taxonomy)**:
   Dismantled unsupported claims of automatic process-level fail-stop behavior. Audited the Presence architecture and confirmed that no independent supervisor thread currently polls queue fault states in real time. Established an honest, forensic taxonomy distinguishing `Faulted Queue`, `Notification Stalled`, `Fail-Closed Subsystem`, and `Process Termination`.
3. **P2C-64 (CPython Proactor Debug-Mode Enforcement)**:
   Addressed the CPython 3.12.7 `BaseProactorEventLoop._write_to_self()` exception path, which invokes `logger.debug` when `self._debug == True`. Enforced a strict configuration invariant: hook startup rejects any event loop where `loop.get_debug() is True`, and debug mode cannot be enabled while the native hook is active.
4. **P2C-65 (Join-Timeout Escalation & Process Exit Semantics)**:
   Disentangled `sys.exit()` and `TerminateProcess`. Proved that `sys.exit()` in a background thread terminates only that thread, while non-daemon threads block interpreter exit. Enforced that `WinEventHookThread` is a daemon thread (`daemon=True`), and specified `os._exit(1)` / Win32 `ExitProcess(1)` as the sole authoritative escalation mechanism invoked by the main-thread supervisor on unrecoverable hook timeouts.
5. **P2C-66 (Lifetime-Bounded Saturating Telemetry Counters)**:
   Replaced unbounded arbitrary-precision Python integers with **32-bit saturating counters** (`UINT32_MAX = 0xFFFFFFFF`). Proved that every telemetry counter object is strictly bounded to $\le 32 \ \text{bytes}$ on 64-bit AMD64. Ten saturating counters consume $\le 320 \ \text{bytes}$ total, guaranteeing that total Python queue heap memory is permanently bounded below $\mathbf{< 16 \ \text{KB}}$ for the lifetime of the process.

All 24 scenarios are implemented in `specs/evidence/phase2c/queue_stress_test.py` and empirically verified passing on Windows 11 AMD64.

---

## 2. Sticky Fault-State Lifecycle (P2C-62)

### 2.1 The Deceptive Recovery Hazard
Specification 55 incorrectly cleared `_faulted = False` inside `drain_and_reset()`:
```python
# FLAWED PATTERN (SPECIFICATION 55)
def drain_and_reset(self):
    with self._lock:
        batch = list(self._deque)
        self._deque.clear()
        self._notification_pending = False
        self._faulted = False  # HAZARD: Cleared terminal fault during normal processing!
        self.events_drained += len(batch)
        return batch
```
If an event-loop failure occurred, the queue transitioned to `_faulted = True`. However, when the consumer drained the remaining batch, `_faulted` was reset to `False`. Subsequent callbacks arriving from the hook thread would then be accepted into the queue, falsely resurrecting an operational state despite the underlying event-loop scheduling failure.

### 2.2 Monotonic Latch Contract
The runtime establishes that `_faulted` is a **monotonic one-way latch**:

$$\text{Operational State} \xrightarrow{\quad \text{Scheduling Failure} \quad} \text{Faulted State} \ (\text{Terminal})$$

1. **One-Way Transition**: Once `_faulted` becomes `True`, no method on that queue instance (`push`, `drain_and_reset`, `len`) can ever reset it to `False`.
2. **Persistent Rejection**: All subsequent producer `push()` invocations are rejected and counted under `events_dropped_faulted`.
3. **Data Preservation**: Unread events already in `_deque` remain available for controlled supervisor draining.
4. **Reconciliation**: `drain_and_reset()` resets `_notification_pending = False` when the deque is emptied, maintaining:
   $$\_notification\_pending == \text{False} \implies \text{queue\_depth} == 0$$
   while leaving `_faulted == True`.
5. **Reinitialization**: To resume operation after a failure, the supervisor must terminate the old hook and consumer tasks, confirm quiescence, and instantiate an entirely **new `BoundedEventQueue` and `ForegroundDispatchCoordinator`** instance.

```python
def drain_and_reset(self) -> list[tuple[int, int, float]]:
    """Drains pending items and clears notification flag. P2C-62: _faulted is STICKY!"""
    with self._lock:
        batch = list(self._deque)
        self._deque.clear()
        self._notification_pending = False
        # CRITICAL (P2C-62): _faulted is NEVER cleared by drain_and_reset()!
        self.events_drained = _sat_inc(self.events_drained, len(batch))
        return batch
```

---

## 3. Producer/Consumer Fault-State Interleavings (P2C-62)

```
+========================================================================================================================+
| Step | Actor / Component | Action Taken                                       | Resulting State & Invariant    |
+========================================================================================================================+
| 1    | Producer (Hook)   | Pushes event 1001; calls call_soon_threadsafe().   | loop.is_closed() -> Exception! |
| 2    | Producer (Hook)   | Calls handle_scheduling_failure().                 | _faulted = True (LATCHED)      |
| 3    | Producer (Hook)   | Subsequent push attempt (event 1002).              | REJECTED; dropped_faulted = 1  |
| 4    | Consumer (Worker) | Wakes or is ordered to drain; calls drain_reset(). | Drains [1001]; pending = False |
| 5    | Consumer (Worker) | Inspects queue._faulted.                           | STILL TRUE! (Not cleared)      |
| 6    | Producer (Hook)   | 5 further push attempts (events 1003–1007).        | ALL REJECTED; dropped_faulted=6|
| 7    | Consumer (Worker) | Second drain_and_reset() call.                     | Returns []; _faulted STILL TRUE|
| 8    | Supervisor        | Stops old hook, joins thread, stops consumer.      | Old lifecycle fully quiesced.  |
| 9    | Supervisor        | Instantiates fresh Coordinator & Queue instance.   | New queue: _faulted = False.   |
| 10   | Producer (Hook)   | Pushes event 2001 to new coordinator instance.     | ACCEPTED (Operational).        |
+========================================================================================================================+
```
This sequence was modeled and verified in Tests 19, 20, and 21 of `queue_stress_test.py`.

---

## 4. Independent Supervisor Capability Assessment & Honest Failure Taxonomy (P2C-63)

### 4.1 Evaluation of Existing Presence Architecture
An audit of `runtime/lifecycle.py` and `runtime/main.py` confirms:
1. `PresenceRuntime` owns `_heartbeat_thread` (`PresenceHeartbeat`), an independent OS thread sleeping on a 5.0-second interval (`self._shutdown_event.wait(timeout=5.0)`) recording database heartbeats.
2. `ForegroundDispatchCoordinator` runs an asyncio consumer worker on an event loop.
3. **Forensic Reality**: There is **no independent supervisor thread** currently polling the queue's `_faulted` flag or monitoring consumer progress at low latency.
4. If the asyncio event loop crashes, hangs, or clears callbacks during a self-pipe socket close race:
   - No asyncio supervisor task can run.
   - The native WinEvent callback cannot wake an independent thread using `threading.Event.set()` without acquiring `self._cond._lock`.
   - The 5.0-second heartbeat thread does not currently inspect internal dispatch coordinator flags.

### 4.2 Honest Failure Taxonomy
Presence formally replaces speculative "fail-stop" claims with four distinct, verifiable states:

```
+────────────────────────────────────────────────────────────────────────────────────────────────────────────+
| Failure State Terminology  | Precise Engineering Definition                                                |
+────────────────────────────────────────────────────────────────────────────────────────────────────────────+
| 1. Faulted Queue           | Queue instance has latched _faulted = True due to caught scheduling failure.  |
|                            | Producer rejects all new incoming events (events_dropped_faulted incremented).|
+------------------------------------------------------------------------------------------------------------+
| 2. Notification Stalled    | Event loop has stopped pumping callbacks (e.g. Proactor socket write swallowed|
|                            | or IOCP hung). Queue has events and notification_pending=True, but worker idle|
+------------------------------------------------------------------------------------------------------------+
| 3. Fail-Closed Subsystem   | Dispatch coordinator stops publishing new foreground snapshots.               |
|                            | Memory remains bounded; stale context is never committed to presence engine.  |
+------------------------------------------------------------------------------------------------------------+
| 4. Process Termination     | Operating system forcibly terminates process via ExitProcess or os._exit.     |
|                            | Guaranteed only when an explicit escalation policy executes on the main thread|
+────────────────────────────────────────────────────────────────────────────────────────────────────────────+
```

Automatic process termination upon internal loop stalls is **not claimed**. The subsystem guarantees **fail-closed containment**: it ceases publishing active context updates and bounds memory, while process exit occurs only when the host lifecycle supervisor or user terminates the process.

---

## 5. CPython Proactor Debug-Mode Configuration (P2C-64)

### 5.1 The Proactor Debug Logging Vector
CPython 3.12.7 `BaseProactorEventLoop._write_to_self()` (`Lib/asyncio/proactor_events.py`, lines 795–802):
```python
    try:
        csock.send(b'\0')
    except OSError:
        if self._debug:
            logger.debug("Fail to write a null byte into the "
                         "self-pipe socket",
                         exc_info=True)
```
If `loop.get_debug() == True`, any `OSError` (e.g. during concurrent socket closure) triggers `logger.debug(...)`, introducing blocking logging locks into the callback path.

### 5.2 Strict Runtime Configuration Contract
To guarantee that `_write_to_self()` is completely non-logging, Presence enforces:
1. **Startup Precondition**: `ForegroundDispatchCoordinator.start_hook()` verifies:
   ```python
   if self.loop is not None and self.loop.get_debug():
       raise ValueError(
           "Cannot start native WinEvent hook with asyncio debug mode enabled. "
           "Debug mode invokes synchronous logger.debug on the Proactor self-pipe error path. "
           "Disable debug mode (loop.set_debug(False)) before registering the hook."
       )
   ```
2. **Active Hook Protection**: `set_debug(True)` is strictly prohibited while `_hook_active == True`:
   ```python
   def set_debug(self, enabled: bool):
       if enabled and self._hook_active:
           raise RuntimeError(
               "Cannot enable asyncio debug mode while native WinEvent hook is active. "
               "Debug mode can only be enabled after hook teardown."
           )
       if self.loop is not None:
           self.loop.set_debug(enabled)
   ```
3. **Restoration**: Debug mode can only be enabled after `stop_hook()` confirms native hook teardown.
This configuration contract is empirically verified in Test 22 of `queue_stress_test.py`.

---

## 6. Join-Timeout Escalation Semantics (P2C-65)

### 6.1 The Fallacy of `sys.exit()` in Multithreaded Runtimes
Specification 55 casually referenced `sys.exit(1) or TerminateProcess`.
- `sys.exit()` simply raises a Python `SystemExit` exception in the calling thread.
- If called in a background thread, `SystemExit` terminates only that thread; the main thread and process remain alive.
- If called on the main thread while a non-daemon thread is alive, Python's runtime shutdown blocks waiting for all non-daemon threads to exit! A hung non-daemon hook thread will deadlock Python interpreter exit indefinitely.

### 6.2 The Authoritative Escalation Contract
Presence establishes a single, unambiguous escalation hierarchy:
1. **Daemon Hook Thread**: `WinEventHookThread` is explicitly configured as a **daemon thread**:
   ```python
   self._hook_thread = threading.Thread(
       target=self._hook_thread_entry,
       name="WinEventHookThread",
       daemon=True
   )
   ```
   If the Python interpreter exits, a stuck hook thread cannot prevent process termination.
2. **Supervisor Ownership**: `PresenceLifecycleSupervisor` operates on the **main thread**.
3. **Join Timeout Handling**: If `thread.join(timeout=1.0)` times out:
   - Supervisor raises `HookShutdownTimeoutError`.
   - Supervisor **aborts** graceful shutdown and preserves the loop so late callbacks do not crash.
4. **Authoritative Escalation Policy**:
   If unrecoverable hook hang escalation is required, Presence selects:
   $$\mathbf{os.\_exit(1)} \quad \text{or Win32} \quad \mathbf{kernel32.ExitProcess(1)}$$
   - `os._exit(1)` immediately terminates the process at the OS kernel level without calling Python `atexit` handlers or waiting for thread joins.
   - **Resource Consequences**: SQLite write-ahead log (WAL) guarantees database integrity via atomic crash-recovery upon next boot; uncommitted transactions are rolled back; OS kernel closes named pipes, handles, and sockets automatically. Unclean shutdown is detected upon restart by `PresenceRuntime.detect_unclean_shutdowns()`.

---

## 7. Bounded Telemetry Strategy & Memory Accounting (P2C-66)

### 7.1 Arbitrary-Precision Integer Growth
Python integers are arbitrary-precision objects. Over extended desktop uptimes (weeks/months), rapid foreground switching can cause raw event counters to exceed $2^{30}$ (32 bytes), $2^{60}$ (36 bytes), $2^{90}$ (40 bytes), and beyond.

### 7.2 The 32-Bit Saturating Counter Strategy
To guarantee permanent, lifetime memory bounds without lock overhead or background reset threads, Presence implements **32-Bit Saturating Counters**:
```python
UINT32_MAX = 0xFFFFFFFF  # 4,294,967,295

def _sat_inc(counter: int, delta: int = 1) -> int:
    """Non-blocking saturating increment."""
    new_val = counter + delta
    return new_val if new_val < UINT32_MAX else UINT32_MAX
```
- In CPython 3.12.7 AMD64, `sys.getsizeof(UINT32_MAX)` is **strictly 32 bytes**.
- Once a counter reaches $4,294,967,295$, it remains saturated at `0xFFFFFFFF`.
- Ten saturating counters consume at most $10 \times 32 = \mathbf{320 \ \text{bytes}}$ for the entire lifetime of the process!

### 7.3 Reproducible Lifetime Memory Accounting (CPython 3.12.7 AMD64)

```
+========================================================================================================================+
| Memory Component Entity                       | Python 3.12 AMD64 Structural Sizing              | Strict Lifetime Max |
+========================================================================================================================+
| 64 Event Records (Upper-Range 3-tuples)       | 64 records * (64 B tuple + 32 B + 32 B + 24 B)   | 9,728 bytes         |
| Deque Container (Capacity 64)                 | collections.deque(maxlen=64) internal block     | 1,288 bytes         |
| Temporary Batch Drain List                    | 64 element pointers + PyListObject header        | 568 bytes           |
| Queue Lock Primitive                          | threading.Lock C-struct wrapper                  | 56 bytes            |
| Core Queue Container Subtotal                 | 9,728 B + 1,288 B + 568 B + 56 B                 | 11,640 bytes        |
+------------------------------------------------------------------------------------------------------------------------+
| BoundedEventQueue Instance Object             | PyObject header + tp_dict pointer                | 48 bytes            |
| Instance __dict__ Table                       | PyDictObject split table                         | 296 bytes           |
| 10 Saturating Telemetry Counter Objects       | 10 counters * max 32 B (saturating at UINT32)    | 320 bytes           |
| Coordination Flags & Instance Primitives      | Booleans (_notification_pending, _faulted)       | ~56 bytes           |
+------------------------------------------------------------------------------------------------------------------------+
| TOTAL LIFETIME PYTHON QUEUE HEAP FOOTPRINT    | Core containers + instance + saturating counters | 12,360 bytes        |
|                                               | Permanent Upper Bound across Process Lifetime    | < 16.0 KB           |
+------------------------------------------------------------------------------------------------------------------------+
| Native Win32 Hook & Thread Resources          | 1 Hook Handle (8 B) + 1 OS Thread (Stack ~64 KB) | ~64 KB - 1 MB       |
| Total Presence Working Set Overhead Ceiling   | Phase 2C incremental working set above Phase 2B  | < 8.0 MB            |
+========================================================================================================================+
```
This guarantees that Python queue data structures will **never exceed 16 KB** on the heap, regardless of uptime or event volume.

---

## 8. Empirical Evidence Suite (24 Scenarios)

The comprehensive verification suite in `specs/evidence/phase2c/queue_stress_test.py` was expanded to 24 scenarios:

```powershell
python specs/evidence/phase2c/queue_stress_test.py
```

### 8.1 Verification Transcript
```text
================== QUEUE CONCURRENCY & STRESS TEST (PHASE 2C CORRECTION 11) ==================
Test 1: Event-loop scheduling succeeds normally...
  -> Passed. Event queued, scheduled, and consumed.
Test 2: Event-loop stops before producer notification...
  -> Passed. Handled stopped loop without crash.
Test 3: Event-loop closes before producer notification...
  -> Passed. Closed loop cleanly trapped; entered faulted state without crash.
Test 4: Scheduling succeeds but loop stops before executing callback...
  -> Passed. Stopped loop preserves queue; shutdown drain recovers event.
Test 5: Scheduling raises RuntimeError without synchronous logging (P2C-54)...
  -> Passed. Controlled fault state established; zero synchronous logging.
Test 6: Faulted queue rejects incoming events and accounts for events_dropped_faulted (P2C-57)...
  -> Passed. Faulted queue rejected events; events_dropped_faulted = 2.
Test 7: Invariant check (notification_pending == False implies queue_depth == 0)...
  -> Passed. Invariant 'pending=False implies queue=0' strictly preserved across failure.
Test 8: CPython BaseProactorEventLoop closed self-pipe swallows socket send (P2C-55, P2C-56)...
  -> Passed. BaseProactorEventLoop error-suppression confirmed; lifecycle prerequisite proved.
Test 9: Strict lifecycle shutdown ordering with consumer quiescence preceding queue reconciliation (P2C-59)...
  -> Passed. Consumer quiesced before supervisor queue reconciliation; zero races.
Test 10: Hook join timeout aborts remaining shutdown sequence and preserves loop (P2C-59)...
  -> Passed. Hook join timeout caught; shutdown safely aborted without closing loop.
Test 11: Repeated hook shutdown and event-loop shutdown ordering (10 cycles)...
  -> Passed. 10 rapid lifecycle iterations completed with zero leaks.
Test 12: Unrelated diagnostic readers never create unhandled queue-lock contention...
  -> Passed. 1,000 diagnostic reads caused exactly 0 contention drops on producer.
Test 13: Composite WindowIdentity validation (all 4 fields match)...
  -> Passed. Verified fresh composite identity successfully committed.
Test 14: Cross-process HWND reuse with different creation ticks rejected (P2C-52)...
  -> Passed. Cross-process HWND recycling attack detected: ticks/PID mismatch rejected.
Test 15: Adversarial same-process, same-thread, same-class HWND reuse limitation evaluated (P2C-60)...
  -> Evaluated. Adversarial same-process/same-class limitation documented & verified.
Test 16: Mid-inspection foreground change cleanly aborted; no snapshot commits after shutdown boundary...
  -> Passed. Mid-inspection switch cleanly aborted; zero commits past shutdown boundary.
Test 17: Memory calculation matches nominal (11,128 B) and upper-range (11,640 B) arithmetic (P2C-61)...
  -> Passed. Nominal: 11128 B (10.87 KB); Upper-range: 11640 B (11.37 KB).
Test 18: Complete telemetry counter consistency including faulted drops (P2C-57, P2C-61)...
  -> Passed. raw_events (40) == drained (40) + dropped (0) + remaining (0).
Test 19: Sticky fault-state persists across drain_and_reset() (P2C-62)...
  -> Passed. Sticky fault flag remained True after queue drain.
Test 20: Faulted queue rejects events over repeated consumer cycles (P2C-62)...
  -> Passed. 5 repeated push/drain attempts all rejected; fault state never cleared.
Test 21: New lifecycle requires quiescence and fresh instance reinitialization (P2C-62)...
  -> Passed. Fresh lifecycle accepts events; old faulted instance safely isolated.
Test 22: Proactor debug mode configuration enforcement (P2C-64)...
  -> Passed. Strict debug mode contract enforced; callback logging mathematically eliminated.
Test 23: Join-timeout escalation and sys.exit background-thread limitation (P2C-65)...
  -> Passed. sys.exit background limitation proved; daemon hook thread verified.
Test 24: Saturating telemetry counters memory bounding (P2C-66)...
  -> Passed. Saturating counters capped at 4294967295; max integer size is 32 B (10 counters = 320 B).
==============================================================================================
>>> ALL 24 QUEUE CONCURRENCY SCENARIOS VERIFIED SUCCESSFULLY! <<<
```

---

## 9. Phase 2C Acceptance Gates Status Matrix

All fifteen architectural acceptance gates (Gates A through O) are evaluated:

| Gate | Focus Area | Architectural Verification Status | Implementation Readiness |
|:---:|---|:---:|:---:|
| **Gate A** | `ShellExecuteExW` Parameter Safety & Flag Precision | **PASSED** | Ready for Phase 2C.2 |
| **Gate B** | Process Handle Ownership & Leak Prevention | **PASSED** | Ready for Phase 2C.2 |
| **Gate C** | Canonical FILETIME Ticks & Integer Arithmetic | **PASSED** | Ready for Phase 2C.1 |
| **Gate D** | Named-Pipe DACL Hardening & Owner Rights Isolation | **PASSED** | Ready for Phase 2C.3 |
| **Gate E** | libuv / Node.js Named-Pipe Interoperability | **PASSED** | Ready for Phase 2C.3 |
| **Gate F** | Cryptographic Protocol & Transcript Process Identity Binding | **PASSED** | Ready for Phase 2C.3 |
| **Gate G** | Bounded WinEvent Dispatch & Sticky Unified Monitor | **PASSED** | Ready for Phase 2C.1 |
| **Gate H** | Supervised Single-Consumer Worker Lifecycle | **PASSED** | Ready for Phase 2C.1 |
| **Gate I** | Best-Effort Composite WindowIdentity Validation | **PASSED** | Ready for Phase 2C.1 |
| **Gate J** | Architectural Decision Record Completeness | **PASSED** | Ready for Phase 2C.0 |
| **Gate K** | Cross-Specification Consistency & Zero Stale References | **PASSED** | Ready for Phase 2C.0 |
| **Gate L** | Lifetime Bounded Saturating Memory Accounting | **PASSED** | Ready for Phase 2C.1 |
| **Gate M** | CPython 3.12.7 Proactor AsyncIO Debug Enforcement | **PASSED** | Ready for Phase 2C.1 |
| **Gate N** | Bounded Callback & Notification Synchronization | **ARCHITECTURE CLOSED / NATIVE PENDING** | Harness verified; Native pending Phase 2C.1 |
| **Gate O** | Owner-Controlled Hook Teardown & Lifecycle Ordering | **ARCHITECTURE CLOSED / NATIVE PENDING** | Harness verified; Native pending Phase 2C.1 |

---

## 10. Final Architecture Freeze & Implementation Decision (Section 12)

1. **Can Phase 2C.0 implementation proceed?**  
   **YES**. Architectural corrections, forensic audits, and design freezes are 100% complete. Phase 2C.0 is formally closed.
2. **Is Phase 2C.1 architecture now frozen?**  
   **YES**. The Phase 2C.1 architecture is frozen. Specifications 41 through 56 define the complete, immutable implementation contract. No further speculative architectural revisions are permitted.
3. **Does a faulted queue remain faulted throughout its lifecycle?**  
   **YES**. The `_faulted` flag is a monotonic one-way latch. It is never cleared by `drain_and_reset()`. A new operational queue can only be obtained through a fresh lifecycle instance after prior hook and consumer termination.
4. **Is Proactor debug-mode logging controlled?**  
   **YES**. Hook startup enforces `loop.get_debug() is False`, and `set_debug(True)` is strictly rejected while the hook is active.
5. **Is process-level termination genuinely guaranteed?**  
   **NO for automatic detection; YES for supervisor escalation.** The runtime acknowledges that internal loop stalls do not automatically trigger process termination. If an unrecoverable hook timeout occurs, the main supervisor escalates authoritatively via `os._exit(1)` rather than relying on `sys.exit()`.
6. **Are telemetry counters lifetime-bounded?**  
   **YES**. All counters use 32-bit saturating arithmetic, permanently bounding counter integer storage to $\le 32 \ \text{bytes}$ and total queue heap overhead to $< 16 \ \text{KB}$ for the life of the process.
7. **Which native integration tests remain pending?**  
   **Only native Win32 OS-level integrations**: Live `SetWinEventHook` in User32, native message pumping via `GetMessageW`, and live window query calls (`GetForegroundWindow`, `GetWindowTextW`). These will be implemented and verified directly in Phase 2C.1 native code.

---

### Final Project Mandate
> **The architecture is frozen. The engineering team is directed to proceed immediately to Phase 2C.1 native implementation. Working Windows code and verified native integration evidence shall supersede further speculative specification passes.**
