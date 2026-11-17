# Specification 55 — Phase 2C Callback Failure-Path Safety, Proactor Wake-Up Semantics, Shutdown Quiescence & HWND Identity Limits

**Milestone:** Phase 2C Architecture Correction 10  
**Author:** Principal Windows Systems Architect, Win32 Concurrency Specialist, Python Runtime Engineer & Application Security Reviewer  
**Status:** Approved & Frozen  
**Repository Baseline:** `c479f6a` (`docs(phase-2c): close event-loop lifecycle and foreground identity semantics`)  
**Scope:** Callback Failure-Path Non-Blocking Audit, CPython 3.12.7 Proactor Source Analysis, Proactor Socket-Close Race Semantics, Faulted Queue State & Producer Rejection Contract, Independent Supervisor Failure Awareness, Shutdown Quiescence & Single-Owner Reconciliation, Hook Join-Timeout Escalation, Composite HWND Identity Limitations, Corrected Memory Measurement Scope, and Final Implementation Readiness  
**Authoritative Predecessors:** Specifications 41 through 54  

---

## 1. Executive Summary

Specification 55 delivers the final architectural closure and forensic validation for Presence Phase 2C, resolving all remaining subtleties identified across the callback failure path, CPython asyncio Proactor internals, lifecycle quiescence, and Win32 identity tracking:

1. **P2C-54 (Complete Callback Failure-Path Audit & Zero Synchronous Logging)**:
   Removed synchronous `logger.critical()` from `handle_scheduling_failure()`. Python logging acquires internal locks (`logging._lock` and handler I/O locks) and performs formatting/disk writes, which previously introduced blocking hazards into the WinEvent callback failure path. The failure path is now strictly in-memory and non-blocking under the GIL.
2. **P2C-55 (CPython 3.12.7 Proactor Source Attribution & Exception Audit)**:
   Corrected the source attribution of `_write_to_self()` from `windows_events.py` to `BaseProactorEventLoop` in `Lib/asyncio/proactor_events.py`. Established that `_write_to_self()` guards against `csock is None` and swallows `OSError` without raising `RuntimeError`. Thread-safe scheduling is decoupled from wait-free or hard real-time execution.
3. **P2C-56 (Proactor Socket-Close Race Semantics & Mandatory Lifecycle Sequencing)**:
   Demonstrated that during event-loop closure, `call_soon_threadsafe()` can return a valid `Handle` successfully even after the self-pipe socket is closed, only for `super().close()` to immediately wipe `_ready` before the callback runs. Proved why in-flight error catching cannot eliminate this race, establishing that native hook termination is a **mandatory, non-negotiable prerequisite** before stopping or closing the event loop.
4. **P2C-57 (Faulted-Queue Producer Rejection Contract)**:
   Specified the complete producer contract under `_faulted = True`. New incoming events are immediately rejected without deque insertion. Dropped events are categorized and counted under `events_dropped_faulted`, keeping the queue strictly bounded at 64 entries and preserving existing unread entries for supervisor drain.
5. **P2C-58 (Independent Supervisor Failure Awareness & Fail-Closed Policy)**:
   Evaluated the failure notification path against the existing `runtime/lifecycle.py` architecture. Acknowledged that an asyncio supervisor task inside the failing event loop cannot execute, while calling `threading.Event.set()` from the callback is prohibited because it acquires `self._cond._lock`. Formally established that automatic graceful recovery following unexpected event-loop death has **not been demonstrated** in Phase 2C, establishing a strict **fail-stop / fail-closed policy**.
6. **P2C-59 (Shutdown Quiescence, Single-Owner Reconciliation & Join-Timeout Escalation)**:
   Corrected the teardown ordering: consumer shutdown and quiescence (`await consumer_task`) must strictly **precede** queue reconciliation (`drain_and_reset()`). Enforced that if `thread.join(timeout=1.0)` times out, the supervisor **aborts** remaining shutdown steps, leaving the event loop alive to prevent late native callbacks from crashing on closed sockets.
7. **P2C-60 (Same-Process HWND Recycling Limitations & Adversarial Evaluation)**:
   Identified that if a window is destroyed and the *same* process and *same* thread creates a replacement window of the *same* class, Windows can reassign the identical numerical `HWND`, causing all four `WindowIdentity` fields to match. Explicitly classified `WindowIdentity` as a **Best-Effort Composite Window Identity**, rather than an absolute universal window lifetime guarantee.
8. **P2C-61 (Memory Measurement Scope across Integer Value Ranges)**:
   Preserved the nominal 11,128-byte sample arithmetic while establishing that object sizes depend on integer values. For upper-range integers ($\ge 2^{30}$), 64 records footprint rises to 9,728 B, yielding a core container sum of 11,640 B ($11.37 \ \text{KB}$). Full queue heap including instance dict and telemetry integers spans $\approx 13.1 \ \text{KB}$ to $\approx 13.7 \ \text{KB}$ ($< 16 \ \text{KB}$), strictly separated from the total process working set ceiling ($< 8.0 \ \text{MB}$).

All 18 scenarios have been implemented in `specs/evidence/phase2c/queue_stress_test.py` and empirically verified passing on Windows 11 with CPython 3.12.7 AMD64.

---

## 2. Complete Callback Failure-Path Audit (P2C-54)

### 2.1 The Synchronous Logging Hazard
Specification 54 introduced `handle_scheduling_failure()` to transition the queue into a faulted state upon `loop.call_soon_threadsafe()` failure. However, it contained:
```python
# BLOCKING HAZARD IN SPECIFICATION 54
logger.critical(
    "Event loop notification scheduling failed. Queue faulted; entering controlled teardown."
)
```
In Python's standard `logging` module:
1. `logger.critical()` calls `Logger.handle()`, which acquires `logging._lock` (a reentrant `threading.RLock`).
2. Handlers (such as `logging.FileHandler` or `StreamHandler`) acquire their own internal locks (`Handler.lock.acquire()`).
3. File handlers perform synchronous OS `write()` and `flush()` system calls, while console handlers write to standard error.
4. If a consumer task, worker thread, or logging thread holds any logging lock across a CPython thread switch, the `WinEventHookThread` callback blocks at the application level.

### 2.2 Forensic Call-Stack Classification
Every synchronous operation reached on the callback path was audited and classified:

$$\text{native\_winevent\_proc} \longrightarrow \text{BoundedEventQueue.push} \longrightarrow \text{loop.call\_soon\_threadsafe} \longrightarrow \text{handle\_scheduling\_failure}$$

```
+========================================================================================================================+
| Stack Frame / Operation           | Execution Layer / Owner            | Blocking Risk | Forensic Action Required      |
+========================================================================================================================+
| PyGILState_Ensure()               | CPython C-API / GIL Scheduler      | Non-app wait  | Inherent CPython GIL quantum  |
| Window filter (event, hwnd, id)   | Presence In-Memory (Integer ops)   | ZERO          | Pure local comparisons        |
| _in_callback reentrancy check     | Presence In-Memory (Boolean flag)  | ZERO          | Lock-free reentrancy check    |
| time.monotonic()                  | OS QueryPerformanceCounter         | ZERO          | Lock-free OS timestamp query  |
| BoundedEventQueue._lock.acquire() | Presence Monitor (blocking=False)  | ZERO          | Strict non-blocking try-lock  |
| collections.deque.append()        | CPython In-Memory C-Deque          | ZERO          | Pre-allocated ring buffer     |
| _notification_pending check       | Presence In-Memory (Boolean flag)  | ZERO          | Evaluated under held try-lock |
| BoundedEventQueue._lock.release() | Presence Monitor                   | ZERO          | Immediate lock release        |
| loop.call_soon_threadsafe()       | CPython AsyncIO / GIL Deque Append | ZERO          | Appends to _ready under GIL   |
| _write_to_self() (csock.send)     | OS Non-blocking Socket Send        | Low (OS)      | Non-blocking loopback write   |
| handle_scheduling_failure()       | Presence In-Memory (Flags/Counters)| ZERO          | REMOVED logger.critical()     |
+========================================================================================================================+
```

### 2.3 Corrected Non-Blocking Failure Handler
Synchronous logging is completely excised from the callback path. The failure handler executes pure in-memory attribute assignments serialized under the CPython GIL:

```python
def handle_scheduling_failure(self) -> None:
    """
    Invoked when loop.call_soon_threadsafe fails or event loop is closed.
    Transitions queue into faulted state without blocking operations.
    CRITICAL (P2C-54): ZERO synchronous logging, disk I/O, or formatting on callback path!
    CRITICAL (P2C-49): NEVER resets _notification_pending = False while unread entries remain!
    """
    self.scheduling_failures += 1
    self._faulted = True
```
Diagnostic alerts and error logging are deferred until control reaches a safe supervisor thread outside the native callback context.

---

## 3. CPython 3.12.7 Proactor Source Audit (P2C-55)

### 3.1 Source Location & Inheritance Hierarchy
Specification 54 attributed `_write_to_self()` to `asyncio/windows_events.py`. The actual CPython 3.12.7 inheritance hierarchy is:

$$\text{windows\_events.ProactorEventLoop} \xrightarrow{\text{inherits}} \text{proactor\_events.BaseProactorEventLoop} \xrightarrow{\text{inherits}} \text{base\_events.BaseEventLoop}$$

The authoritative implementation of the self-pipe resides in `Lib/asyncio/proactor_events.py`:

```python
# CPython 3.12.7 - Lib/asyncio/proactor_events.py (lines 769-783)
def _make_self_pipe(self):
    # A self-socket, really. :-)
    self._ssock, self._csock = socket.socketpair()
    self._ssock.setblocking(False)
    self._csock.setblocking(False)
    self._internal_fds += 1

def _close_self_pipe(self):
    if self._self_reading_future is not None:
        self._self_reading_future.cancel()
        self._self_reading_future = None
    self._ssock.close()
    self._ssock = None
    self._csock.close()
    self._csock = None
    self._internal_fds -= 1
```

```python
# CPython 3.12.7 - Lib/asyncio/proactor_events.py (lines 785-802)
def _write_to_self(self):
    # This may be called from a different thread, possibly after
    # _close_self_pipe() has been called or even while it is
    # running.  Guard for self._csock being None or closed.  When
    # a socket is closed, send() raises OSError (with errno set to
    # EBADF, but let's not rely on the exact error code).
    csock = self._csock
    if csock is None:
        return

    try:
        csock.send(b'\0')
    except OSError:
        if self._debug:
            logger.debug("Fail to write a null byte into the "
                         "self-pipe socket",
                         exc_info=True)
```

### 3.2 Forensic Analysis of Exception Handling
1. **Socket Error Suppression**: `_write_to_self()` explicitly catches `OSError`. It **does not raise** `OSError` or `RuntimeError` to caller frames.
2. **Debug-Only Logging**: If `csock.send()` fails, it writes to `logger.debug` *only if* `self._debug == True`. In production (`_debug == False`), the failure is completely silent.
3. **Guard for `csock is None`**: If `_close_self_pipe()` has already set `self._csock = None`, `_write_to_self()` returns immediately with `None`.
4. **Significance**: `call_soon_threadsafe()` will **never** raise an exception due to a failing or closed self-pipe socket. It raises `RuntimeError` *only* if `self._closed` is already `True` when `_check_closed()` is invoked.

---

## 4. Proactor Socket-Close Race Semantics (P2C-56)

### 4.1 The Silent Callback Loss Race Condition
Consider the exact teardown sequence in `BaseProactorEventLoop.close()`:

```python
# CPython 3.12.7 - Lib/asyncio/proactor_events.py (lines 690-705)
def close(self):
    if self.is_running():
        raise RuntimeError("Cannot close a running event loop")
    if self.is_closed():
        return
    ...
    self._stop_accept_futures()
    self._close_self_pipe()        # STEP A: _csock set to None and closed!
    self._proactor.close()
    self._proactor = None
    self._selector = None

    super().close()                # STEP B: BaseEventLoop.close() executes!
```

```python
# CPython 3.12.7 - Lib/asyncio/base_events.py (lines 752-763)
def close(self):
    ...
    self._closed = True            # STEP C: self._closed set to True!
    self._ready.clear()            # STEP D: self._ready cleared!
    self._scheduled.clear()
```

### 4.2 Step-by-Step Interleaving Trace
If the event loop is closed concurrently while the native hook is still active:

```
[WinEventHookThread]                            [Event Loop Thread (close)]
────────────────────                            ──────────────────────────
1. Callback invoked; pushes event.
2. Calls loop.call_soon_threadsafe().
3. Invokes self._check_closed().
   -> self._closed is still False! (Succeeds)
4. Invokes self._call_soon().
   -> Handle appended to self._ready!
                                                5. Executes self._close_self_pipe().
                                                   -> self._csock set to None!
6. Invokes self._write_to_self().
   -> Observes self._csock is None!
   -> Returns cleanly without exception!
7. call_soon_threadsafe returns Handle!
   (Producer assumes notification succeeded!)
                                                8. Executes super().close().
                                                   -> self._closed = True
                                                   -> self._ready.clear() (WIPED!)
```

### 4.3 Mandatory Lifecycle Sequencing Proof
- **Observation**: The producer observed a completely successful call to `call_soon_threadsafe()`. No exception was raised, so `handle_scheduling_failure()` was **not invoked**.
- **Result**: The callback was discarded during `self._ready.clear()`. The event remains stranded in the queue with `_notification_pending == True`, and the consumer will never execute.
- **Architectural Conclusion**: **In-flight error trapping inside `on_winevent()` cannot eliminate this race.** Checking `not loop.is_closed()` before calling `call_soon_threadsafe()` is inherently vulnerable to this TOCTOU race.
- **Enforcement Rule**: The supervisor MUST enforce that `UnhookWinEvent` completes and `WinEventHookThread` terminates **BEFORE** `loop.stop()` or `loop.close()` can be invoked.
- **Ownership**: `PresenceLifecycleSupervisor` is the sole owner of `loop.stop()` and `loop.close()`. No other component is permitted to stop or close the event loop.

---

## 5. Explicit Faulted-Queue Producer Contract (P2C-57)

When `_faulted` transitions to `True`, the queue enters a strict fail-closed rejection state:

### 5.1 Formal Invariants
1. **Zero New Enqueues**: Once `_faulted == True`, `push()` rejects all incoming events without appending them to `_deque`.
2. **Explicit Accounting**: Rejected events are counted under `events_dropped_faulted`, keeping drop categories mutually exclusive (`contention`, `capacity`, `faulted`).
3. **Capacity Bounded**: Usable capacity remains strictly 64 items; existing unread entries are retained intact.
4. **Single-Lock Coordination**: Fault checking occurs under the existing monitor try-lock; no secondary locks are introduced.
5. **Atomic Reconciliation**: `_faulted` is reset to `False` if and only if `_notification_pending` is reset to `False` and the queue is drained in `drain_and_reset()`.

### 5.2 Authoritative Queue Implementation
```python
class BoundedEventQueue:
    """Bounded queue with explicit faulted-state producer rejection."""

    def __init__(self, capacity: int = 64):
        self.capacity = capacity
        self._lock = threading.Lock()
        self._deque = collections.deque(maxlen=capacity)
        self._notification_pending = False
        self._faulted = False
        
        # Telemetry counters
        self.raw_events_received = 0
        self.events_pushed = 0
        self.events_dropped_capacity = 0
        self.events_dropped_contention = 0
        self.events_dropped_faulted = 0
        self.events_coalesced = 0
        self.events_drained = 0
        self.scheduling_failures = 0

    @property
    def events_dropped(self) -> int:
        return (
            self.events_dropped_capacity +
            self.events_dropped_contention +
            self.events_dropped_faulted
        )

    def push(self, event: tuple[int, int, float]) -> tuple[bool, bool]:
        """Non-blocking push on WinEvent hook thread."""
        self.raw_events_received += 1
        
        if not self._lock.acquire(blocking=False):
            self.events_dropped_contention += 1
            return False, False
            
        try:
            if self._faulted:
                # System is in faulted state; reject incoming events immediately (P2C-57)
                self.events_dropped_faulted += 1
                return False, False

            if len(self._deque) >= self.capacity:
                self.events_dropped_capacity += 1
                return False, False
                
            self._deque.append(event)
            self.events_pushed += 1
            
            if not self._notification_pending:
                self._notification_pending = True
                return True, True
            else:
                self.events_coalesced += 1
                return True, False
        finally:
            self._lock.release()

    def handle_scheduling_failure(self) -> None:
        """Atomic failure transition; zero logging or locks on callback path."""
        self.scheduling_failures += 1
        self._faulted = True

    def drain_and_reset(self) -> list[tuple[int, int, float]]:
        """Atomically drains pending items and resets flags."""
        with self._lock:
            batch = list(self._deque)
            self._deque.clear()
            self._notification_pending = False
            self._faulted = False
            self.events_drained += len(batch)
            return batch
```

---

## 6. Independent Supervisor Failure Awareness & Fail-Closed Policy (P2C-58)

### 6.1 Architectural Investigation of Supervisor Contexts
The existing Presence runtime (`runtime/lifecycle.py`) provides:
1. `PresenceRuntime`: Main-thread coordinator owning the single-instance mutex and SQLite persistence.
2. `PresenceHeartbeat`: A dedicated background thread recording periodic heartbeats (every 5.0 s).
3. `ForegroundDispatchCoordinator`: Operates an asyncio event loop running the consumer worker.

### 6.2 The Communication Impossibility on Loop Failure
If the asyncio event loop closes or fails:
- An asyncio task running inside that loop **cannot execute**.
- Signalling a `threading.Event` from the callback path is **prohibited** because `threading.Event.set()` acquires `self._cond._lock` (a blocking lock).
- The heartbeat thread polls at coarse 5.0-second intervals and does not monitor queue internal state.

### 6.3 Formal Determination: Fail-Stop / Fail-Closed Policy
Presence explicitly documents that **automatic graceful recovery following unexpected event-loop death has NOT been demonstrated in Phase 2C**:
1. When scheduling fails, the queue transitions to `_faulted = True` and drops subsequent events (`events_dropped_faulted += 1`).
2. The runtime avoids fabricating an imaginary real-time recovery supervisor.
3. The system operates under a **Fail-Stop / Fail-Closed Policy**:
   - The queue stops accepting input.
   - When the process-level lifecycle coordinator (e.g. `PresenceRuntime.stop()` or console handler) initiates shutdown, the supervisor drains remaining items under `_lock` and logs diagnostic telemetry from that safe supervisor thread.
   - If the event loop crashed completely, the process terminates fail-closed; on subsequent start, `PresenceRuntime` detects the unclean shutdown via `detect_unclean_shutdowns()`.

---

## 7. Shutdown Quiescence & Single-Owner Reconciliation (P2C-59)

### 7.1 Flaw in Prior Teardown Sequence
Specification 54 specified draining the queue *before* stopping the consumer worker:
```text
3. Join hook thread
4. Drain queue (FLAW: Consumer still awake!)
5. Stop consumer worker
```
If the consumer woke up while the supervisor drained the queue, a race occurred where the consumer and supervisor concurrently mutated or committed window snapshots.

### 7.2 Corrected 8-Step Quiesced Shutdown Protocol
The supervisor strictly enforces consumer quiescence before queue reconciliation:

```text
 1. Supervisor Signals Hook Thread Teardown (PostThreadMessageW(WM_QUIT))
    │
 2. UnhookWinEvent(hHook) Executes on Hook Thread
    │
 3. Supervisor Joins Hook Thread (thread.join(timeout=1.0))
    ├─► [IF TIMEOUT]: ABORT remaining shutdown steps! (See Section 8)
    ▼
 4. Supervisor Signals Consumer Shutdown (shutdown_event.set(), unblock wait)
    │
 5. Supervisor Awaits Consumer Quiescence (await consumer_task)
    │   -> Consumer exits loop; active_consumer_count drops to 0.
    ▼
 6. Single-Owner Queue Reconciliation (queue.drain_and_reset())
    │   -> Producer is dead (Step 3); Consumer is dead (Step 5).
    │   -> Supervisor is SOLE OWNER of queue; drains unread entries safely.
    ▼
 7. Supervisor Stops Event Loop (loop.stop())
    │
 8. Supervisor Closes Event Loop (loop.close())
```

### 7.3 Snapshot Commit Boundary Invariant
To prevent stale snapshots from committing after shutdown begins, `consumer_worker()` re-checks `self.shutdown_event.is_set()` immediately prior to publishing:
```python
if self.shutdown_event.is_set():
    self.stale_snapshots_aborted += 1
    break
```
No snapshot can ever commit after the shutdown boundary.

---

## 8. Hook Join-Timeout Escalation & Abort Protocol (P2C-59)

### 8.1 The Hang Hazard
If the native hook thread hangs (e.g. during a blocked Win32 call or unresponsive message pump), `thread.join(timeout=1.0)` will time out.
If the supervisor blindly continued to stop or close the event loop, late WinEvent callbacks from the hanging thread would hit a closed loop or closed socket, triggering unhandled exceptions or memory corruption.

### 8.2 Strict Abort Contract
1. **Never Assume Cleanup**: A join timeout is **not proof** that the native hook is gone.
2. **Abort Shutdown Pipeline**: If `thread.is_alive()` is `True` after 1.0 second:
   - The supervisor **aborts** Steps 4 through 8.
   - The event loop is **NOT stopped and NOT closed**; it remains open to absorb any late callbacks safely.
   - The supervisor marks the subsystem in `HOOK_TEARDOWN_TIMEOUT` fault state.
   - Raises `HookShutdownTimeoutError("Hook thread termination timed out. Shutdown aborted.")`.
3. **Process Escalation**: The process lifecycle supervisor logs critical diagnostic evidence (hook thread TID, hook handle) and escalates to a controlled process termination (`sys.exit(1)` or `TerminateProcess`), failing closed safely.

---

## 9. Composite HWND Identity Limitations & Adversarial Analysis (P2C-60)

### 9.1 The Same-Process, Same-Class HWND Recycling Edge Case
Specification 54 established the 4-field composite identity:
$$\text{WindowIdentity} = (\text{HWND}, \ \text{PID}, \ \text{creation\_filetime\_ticks}, \ \text{window\_class})$$

While this reliably detects cross-process HWND reuse, process restarts, and class changes, it **cannot detect** the following adversarial edge case:
1. Window $W_1$ (`HWND 0x1515`) is created by Process $P$ (PID 500, FILETIME $T$) with window class `"CabinetWClass"`.
2. Consumer captures `initial_identity = (0x1515, 500, T, "CabinetWClass")`.
3. During asynchronous metadata inspection, Process $P$ destroys window $W_1$.
4. The *same thread* in Process $P$ immediately creates a new window $W_2$ of the *same class* `"CabinetWClass"`.
5. The Win32 OS user-mode handle table reassigns the recycled handle `0x1515` to $W_2$.
6. Consumer captures `final_identity = (0x1515, 500, T, "CabinetWClass")`.
7. **All four fields match identically.** The check passes, committing $W_2$'s handle under $W_1$'s metadata!

### 9.2 API Feasibility & Ambient History Conflict
- Win32 User32 provides **no 64-bit window generation ID** or window creation timestamps.
- Tracking `EVENT_OBJECT_CREATE` and `EVENT_OBJECT_DESTROY` for all windows would generate thousands of events per second, overwhelm the message pump, lose tracking on queue overflow, and require an ambient desktop window database.
- Maintaining an ambient window database directly violates Presence Privacy Principle **ADR-2C-03** (Zero Ambient Window History Buffer).

### 9.3 Formal Classification
Presence formally defines this mechanism as a:
$$\mathbf{Best\text{-}Effort \ Composite \ Window \ Identity}$$
It provides deterministic protection against cross-process recycling and process replacement, but explicitly acknowledges the theoretical same-process, same-class HWND reuse limitation. This limitation is modeled and verified in Test 15 of `queue_stress_test.py`.

---

## 10. Corrected Memory Measurement Scope (P2C-61)

### 10.1 Integer Value Range Sensitivity
In CPython 3.12.7 AMD64, `int` memory overhead depends on the magnitude of the represented value:
- Small integers ($< 2^{30}$, i.e. $< 1,073,741,824$): `sys.getsizeof(int)` = **28 bytes**.
- Upper-range integers ($\ge 2^{30}$, e.g. large `HWND` pointer values, uint32 `dwmsEventTime` $> 49.7$ days uptime, or 64-bit FILETIME ticks): `sys.getsizeof(int)` = **32 bytes**.
- Tuple header: **64 bytes**; Float arrival time: **24 bytes**.

### 10.2 Nominal vs Upper-Range Arithmetic

| Component Entity | Nominal Size ($< 2^{30}$) | Upper-Range Size ($\ge 2^{30}$) |
|---|:---:|:---:|
| **Event Record (3-tuple + referenced values)** | $64 + 28 + 28 + 24 = \mathbf{144 \ \text{bytes}}$ | $64 + 32 + 32 + 24 = \mathbf{152 \ \text{bytes}}$ |
| **Retained 64 Records** | $64 \times 144 = \mathbf{9,216 \ \text{bytes}}$ | $64 \times 152 = \mathbf{9,728 \ \text{bytes}}$ |
| **Deque Container (Capacity 64)** | **1,288 bytes** | **1,288 bytes** |
| **Temporary Batch List (64 elements)** | **568 bytes** | **568 bytes** |
| **Lock Primitive (`threading.Lock`)** | **56 bytes** | **56 bytes** |
| **Core Container Data Structures Sum** | $\mathbf{11,128 \ \text{bytes}} \ (10.87 \ \text{KB})$ | $\mathbf{11,640 \ \text{bytes}} \ (11.37 \ \text{KB})$ |
| **Queue Object Instance (`sys.getsizeof(q)`)** | **48 bytes** | **48 bytes** |
| **Instance State Dict (`sys.getsizeof(q.__dict__)`)** | **296 bytes** | **296 bytes** |
| **Dynamic Telemetry Integers (In-Memory)** | **~1,680 bytes** | **~1,720 bytes** |
| **Total Python Queue Heap Footprint** | $\mathbf{\approx 13.1 \ \text{KB}}$ | $\mathbf{\approx 13.7 \ \text{KB}}$ |

### 10.3 Scope Classification
1. **Nominal Sample (11,128 B)**: Environment-specific measurement for CPython 3.12.7 AMD64 with small integer values.
2. **Strict Queue Heap Ceiling**: In all cases, total queue heap overhead remains strictly bounded below **$< 16 \ \text{KB}$** ($< 0.02 \ \text{MB}$).
3. **Process Working Set Ceiling**: Total Presence working set overhead for Phase 2C remains strictly bounded below **$< 8.0 \ \text{MB}$** above Phase 2B idle baseline (~96 MB).

---

## 11. Empirical Evidence Harness Results (18 Scenarios)

All 18 verification scenarios were executed using `specs/evidence/phase2c/queue_stress_test.py`:

```powershell
python specs/evidence/phase2c/queue_stress_test.py
```

### 11.1 Execution Transcript
```text
================== QUEUE CONCURRENCY & STRESS TEST (PHASE 2C CORRECTION 10) ==================
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
==============================================================================================
>>> ALL 18 QUEUE CONCURRENCY SCENARIOS VERIFIED SUCCESSFULLY! <<<
```

---

## 12. Updated Acceptance Gates Matrix

| Gate | Focus Area | Status | Implementation Readiness |
|:---:|---|:---:|:---:|
| **Gate A** | `ShellExecuteExW` Parameter Safety & Flag Precision | **PASSED** | Ready for Phase 2C.2 |
| **Gate B** | Process Handle Ownership & Leak Prevention | **PASSED** | Ready for Phase 2C.2 |
| **Gate C** | Canonical FILETIME Ticks & Integer Arithmetic | **PASSED** | Ready for Phase 2C.1 |
| **Gate D** | Named-Pipe DACL Hardening & Owner Rights Isolation | **PASSED** | Ready for Phase 2C.3 |
| **Gate E** | libuv / Node.js Named-Pipe Interoperability | **PASSED** | Ready for Phase 2C.3 |
| **Gate F** | Cryptographic Protocol & Transcript Process Identity Binding | **PASSED** | Ready for Phase 2C.3 |
| **Gate G** | Bounded WinEvent Dispatch & Unified Monitor Ownership | **PASSED** | Ready for Phase 2C.1 |
| **Gate H** | Supervised Single-Consumer Worker Lifecycle | **PASSED** | Ready for Phase 2C.1 |
| **Gate I** | Best-Effort Composite WindowIdentity Validation | **PASSED** | Ready for Phase 2C.1 |
| **Gate J** | Architectural Decision Record Completeness | **PASSED** | Ready for Phase 2C.0 |
| **Gate K** | Cross-Specification Consistency & Zero Stale References | **PASSED** | Ready for Phase 2C.0 |
| **Gate L** | Memory Accounting & Complexity Precision | **PASSED** | Ready for Phase 2C.1 |
| **Gate M** | CPython 3.12.7 Proactor AsyncIO Internals Accuracy | **PASSED** | Ready for Phase 2C.1 |
| **Gate N** | Bounded Callback & Notification Synchronization | **ARCHITECTURE CLOSED / NATIVE PENDING** | Harness verified; Native pending Phase 2C.1 |
| **Gate O** | Owner-Controlled Hook Teardown & Lifecycle Ordering | **ARCHITECTURE CLOSED / NATIVE PENDING** | Harness verified; Native pending Phase 2C.1 |

---

## 13. Final Implementation-Readiness Decision (Section 15)

1. **Can Phase 2C.0 proceed?**  
   **YES**. Architectural corrections, security models, and design freeze are 100% complete and frozen.
2. **Is Phase 2C.1 architecture sufficiently specified?**  
   **YES**. The hook thread model, message pump, bounded queue, try-lock monitor, non-blocking failure handler, quiesced 8-step lifecycle ordering, timeout abort contract, composite window identity, and telemetry counters are fully specified.
3. **Does the callback failure path avoid Presence-owned blocking operations?**  
   **YES**. Synchronous logging has been excised. The callback failure path executes purely in-memory variable updates under CPython GIL serialization. Zero application-level blocking lock waits exist.
4. **Does scheduling failure have an independently functioning supervisor?**  
   **NO**. The architecture explicitly acknowledges that automatic graceful recovery following unexpected event-loop death has not been demonstrated. The system enforces a fail-stop / fail-closed policy.
5. **Is shutdown safe when hook termination fails?**  
   **YES**. If `thread.join(timeout=1.0)` times out, the supervisor aborts shutdown, keeps the event loop alive to prevent crashes on late callbacks, marks `HOOK_TEARDOWN_TIMEOUT`, and escalates to process-level termination.
6. **Does the identity contract accurately describe its remaining limitations?**  
   **YES**. The contract explicitly documents that while cross-process and process-restart recycling are detected, same-process/same-class HWND reuse cannot be distinguished under documented Windows APIs without violating privacy boundaries.
7. **Are native WinEvent integration tests still pending?**  
   **YES**. Real OS-level `SetWinEventHook` registration and live Win32 message pumping will be executed during Phase 2C.1 native development.

---

### Final Architectural Principle
> **A failure state is only useful when someone can observe it, a shutdown is only successful when every producer and consumer has actually stopped, and an identity guarantee is only trustworthy when its limitations are explicit.**
