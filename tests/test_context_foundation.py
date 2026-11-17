"""Unit Tests for Context Foundation Subsystem (Phase 2C.0)
Validates deep immutability, canonical FILETIME calculations, sensitivity ordering,
TTL expiration, lifecycle state transitions, and single-snapshot retention.
"""

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
import threading
import time
import unittest

from runtime.context.contracts import (
    ContextItemImmutableError,
    ContextSourceState,
    InvalidStateTransitionError,
)
from runtime.context.expiration import (
    filter_unexpired_items,
    is_item_expired,
    is_snapshot_expired,
)
from runtime.context.manager import ContextManager, MockContextSource
from runtime.context.models import (
    FILETIME_UNIX_EPOCH_OFFSET_TICKS,
    ContextItem,
    ContextSensitivity,
    ContextSnapshot,
    WindowIdentity,
    filetime_ticks_to_unix_parts,
    filetime_ticks_to_utc_datetime,
    filetime_to_ticks,
)
from runtime.events import CONTEXT_SNAPSHOT_CREATED, EventBus


class ArbitraryCustomObject:
    def __init__(self, val):
        self.val = val


class TestContextFoundation(unittest.TestCase):
    """20 targeted unit tests for Phase 2C.0 Context Foundation."""

    def test_1_context_sensitivity_levels_and_ordering(self):
        """1. Verifies hierarchical ordering and comparison of sensitivity levels."""
        self.assertTrue(ContextSensitivity.PUBLIC_CONTEXT < ContextSensitivity.PERSONAL_CONTEXT)
        self.assertTrue(ContextSensitivity.PERSONAL_CONTEXT < ContextSensitivity.SENSITIVE_CONTEXT)
        self.assertTrue(ContextSensitivity.SENSITIVE_CONTEXT < ContextSensitivity.PROTECTED_CONTEXT)
        self.assertTrue(ContextSensitivity.PUBLIC_CONTEXT <= ContextSensitivity.PUBLIC_CONTEXT)
        self.assertFalse(ContextSensitivity.PROTECTED_CONTEXT < ContextSensitivity.PUBLIC_CONTEXT)

        sorted_levels = sorted([
            ContextSensitivity.PROTECTED_CONTEXT,
            ContextSensitivity.PUBLIC_CONTEXT,
            ContextSensitivity.SENSITIVE_CONTEXT,
            ContextSensitivity.PERSONAL_CONTEXT,
        ])
        expected = [
            ContextSensitivity.PUBLIC_CONTEXT,
            ContextSensitivity.PERSONAL_CONTEXT,
            ContextSensitivity.SENSITIVE_CONTEXT,
            ContextSensitivity.PROTECTED_CONTEXT,
        ]
        self.assertEqual(sorted_levels, expected)

    def test_2_context_item_deep_immutability(self):
        """2. Verifies that ContextItem attributes cannot be modified after instantiation."""
        item = ContextItem(
            key="test.key",
            value="sample_val",
            sensitivity=ContextSensitivity.PUBLIC_CONTEXT,
            source_id="test_source",
            observed_utc=time.time(),
            observed_monotonic=time.monotonic(),
        )
        with self.assertRaises(FrozenInstanceError):
            item.value = "mutated_val"  # type: ignore

    def test_3_context_item_recursive_tuple_conversion(self):
        """3. Verifies that mutable lists and dicts are converted to deeply immutable tuples."""
        raw_list = ["item1", ["subitem1", "subitem2"], {"k": [1, 2]}]
        item = ContextItem(
            key="nested.collection",
            value=raw_list,
            sensitivity=ContextSensitivity.PERSONAL_CONTEXT,
            source_id="test_source",
            observed_utc=time.time(),
            observed_monotonic=time.monotonic(),
        )
        self.assertIsInstance(item.value, tuple)
        self.assertIsInstance(item.value[1], tuple)
        self.assertIsInstance(item.value[2], tuple)
        self.assertEqual(item.value[0], "item1")
        self.assertEqual(item.value[1], ("subitem1", "subitem2"))
        self.assertEqual(item.value[2], (("k", (1, 2)),))

    def test_4_context_item_rejects_arbitrary_mutable_objects(self):
        """4. Verifies that unapproved mutable or arbitrary opaque objects are rejected."""
        custom_obj = ArbitraryCustomObject(42)
        with self.assertRaises(ContextItemImmutableError):
            ContextItem(
                key="unsupported.obj",
                value=custom_obj,
                sensitivity=ContextSensitivity.PUBLIC_CONTEXT,
                source_id="test_source",
                observed_utc=time.time(),
                observed_monotonic=time.monotonic(),
            )

    def test_5_context_item_alias_protection(self):
        """5. Verifies mutating the original input structure does not mutate ContextItem value."""
        mutable_input = [1, 2, [3, 4]]
        item = ContextItem(
            key="aliased.input",
            value=mutable_input,
            sensitivity=ContextSensitivity.PUBLIC_CONTEXT,
            source_id="test_source",
            observed_utc=time.time(),
            observed_monotonic=time.monotonic(),
        )
        mutable_input[0] = 999
        mutable_input[2].append(5)  # type: ignore

        self.assertEqual(item.value, (1, 2, (3, 4)))

    def test_6_window_identity_frozen(self):
        """6. Verifies WindowIdentity is frozen and enforces strict type constraints."""
        identity = WindowIdentity(
            hwnd=0x1234,
            process_id=5678,
            process_creation_filetime_ticks=133000000000000000,
            window_class="CabinetWClass",
            application_name="explorer.exe",
            observed_monotonic=time.monotonic(),
            snapshot_id="snap-01",
        )
        with self.assertRaises(FrozenInstanceError):
            identity.hwnd = 0x9999  # type: ignore

        with self.assertRaises(TypeError):
            WindowIdentity(
                hwnd="invalid_string_hwnd",  # type: ignore
                process_id=5678,
                process_creation_filetime_ticks=133000000000000000,
                window_class="CabinetWClass",
            )

    def test_7_filetime_canonical_conversions(self):
        """7. Verifies pure integer arithmetic for FILETIME tick and epoch conversions."""
        high = 0x01DA6521
        low = 0xA2345678
        ticks = filetime_to_ticks(high, low)
        self.assertEqual(ticks, (high << 32) | low)

        # FILETIME epoch offset: 116444736000000000 ticks = 1970-01-01 00:00:00 UTC
        unix_100ns, unix_ns, unix_sec = filetime_ticks_to_unix_parts(FILETIME_UNIX_EPOCH_OFFSET_TICKS)
        self.assertEqual(unix_100ns, 0)
        self.assertEqual(unix_ns, 0)
        self.assertEqual(unix_sec, 0)

        dt = filetime_ticks_to_utc_datetime(FILETIME_UNIX_EPOCH_OFFSET_TICKS)
        self.assertEqual(dt, datetime(1970, 1, 1, 0, 0, 0, tzinfo=timezone.utc))

        # Test known timestamp: 1 second after Unix epoch (10,000,000 ticks)
        one_sec_ticks = FILETIME_UNIX_EPOCH_OFFSET_TICKS + 10_000_000
        _, _, unix_sec_1 = filetime_ticks_to_unix_parts(one_sec_ticks)
        self.assertEqual(unix_sec_1, 1)
        dt_1 = filetime_ticks_to_utc_datetime(one_sec_ticks)
        self.assertEqual(dt_1, datetime(1970, 1, 1, 0, 0, 1, tzinfo=timezone.utc))

    def test_8_filetime_submicrosecond_truncation_documentation(self):
        """8. Verifies sub-microsecond truncation in datetime without altering canonical ticks."""
        # 5 sub-microsecond ticks = 500 ns
        sub_micro_ticks = FILETIME_UNIX_EPOCH_OFFSET_TICKS + 5
        unix_100ns, unix_ns, _ = filetime_ticks_to_unix_parts(sub_micro_ticks)
        self.assertEqual(unix_100ns, 5)
        self.assertEqual(unix_ns, 500)

        dt = filetime_ticks_to_utc_datetime(sub_micro_ticks)
        # datetime has microsecond resolution, so 500 ns (0.5 us) truncates to 0 microsecond
        self.assertEqual(dt.microsecond, 0)
        # But canonical ticks are preserved
        self.assertEqual(sub_micro_ticks - FILETIME_UNIX_EPOCH_OFFSET_TICKS, 5)

    def test_9_context_snapshot_immutability(self):
        """9. Verifies ContextSnapshot immutability and rejection of item modifications."""
        item = ContextItem(
            key="test.item",
            value="val",
            sensitivity=ContextSensitivity.PUBLIC_CONTEXT,
            source_id="src",
            observed_utc=time.time(),
            observed_monotonic=time.monotonic(),
        )
        snapshot = ContextSnapshot(
            snapshot_id="snap-001",
            created_utc=time.time(),
            created_monotonic=time.monotonic(),
            items=(item,),
        )
        with self.assertRaises(FrozenInstanceError):
            snapshot.snapshot_id = "mutated-id"  # type: ignore

    def test_10_context_snapshot_filter_by_sensitivity(self):
        """10. Verifies sensitivity-based filtering of items in ContextSnapshot."""
        pub_item = ContextItem("pub", "public_val", ContextSensitivity.PUBLIC_CONTEXT, "src", 0.0, 0.0)
        per_item = ContextItem("per", "personal_val", ContextSensitivity.PERSONAL_CONTEXT, "src", 0.0, 0.0)
        sen_item = ContextItem("sen", "sensitive_val", ContextSensitivity.SENSITIVE_CONTEXT, "src", 0.0, 0.0)
        pro_item = ContextItem("pro", "protected_val", ContextSensitivity.PROTECTED_CONTEXT, "src", 0.0, 0.0)

        snapshot = ContextSnapshot("snap", 0.0, 0.0, items=(pub_item, per_item, sen_item, pro_item))

        # Filter max PUBLIC_CONTEXT
        pub_only = snapshot.filter_by_sensitivity(ContextSensitivity.PUBLIC_CONTEXT)
        self.assertEqual(tuple(i.key for i in pub_only), ("pub",))

        # Filter max PERSONAL_CONTEXT
        pub_and_per = snapshot.filter_by_sensitivity(ContextSensitivity.PERSONAL_CONTEXT)
        self.assertEqual(tuple(i.key for i in pub_and_per), ("pub", "per"))

        # Filter max PROTECTED_CONTEXT
        all_items = snapshot.filter_by_sensitivity(ContextSensitivity.PROTECTED_CONTEXT)
        self.assertEqual(len(all_items), 4)

    def test_11_context_snapshot_get_item(self):
        """11. Verifies get_item retrieves the requested item or returns None."""
        item = ContextItem("lookup.key", "found", ContextSensitivity.PUBLIC_CONTEXT, "src", 0.0, 0.0)
        snapshot = ContextSnapshot("snap", 0.0, 0.0, items=(item,))

        self.assertIsNotNone(snapshot.get_item("lookup.key"))
        self.assertEqual(snapshot.get_item("lookup.key").value, "found")
        self.assertIsNone(snapshot.get_item("missing.key"))

    def test_12_monotonic_ttl_item_expiration(self):
        """12. Verifies monotonic TTL item expiration evaluation."""
        base_m = 1000.0
        item_with_ttl = ContextItem("exp", "val", ContextSensitivity.PUBLIC_CONTEXT, "src", 0.0, base_m, ttl_seconds=5.0)
        item_no_ttl = ContextItem("inf", "val", ContextSensitivity.PUBLIC_CONTEXT, "src", 0.0, base_m, ttl_seconds=None)

        # At base_m + 2.0s (within 5.0s TTL)
        self.assertFalse(is_item_expired(item_with_ttl, current_monotonic=base_m + 2.0))
        self.assertFalse(is_item_expired(item_no_ttl, current_monotonic=base_m + 2.0))

        # At base_m + 5.1s (exceeded 5.0s TTL)
        self.assertTrue(is_item_expired(item_with_ttl, current_monotonic=base_m + 5.1))
        # Non-expiring item still fresh
        self.assertFalse(is_item_expired(item_no_ttl, current_monotonic=base_m + 5.1))

    def test_13_monotonic_ttl_snapshot_expiration(self):
        """13. Verifies snapshot-level TTL evaluation and unexpired item filtering."""
        base_m = 2000.0
        item1 = ContextItem("i1", "v1", ContextSensitivity.PUBLIC_CONTEXT, "src", 0.0, base_m, ttl_seconds=3.0)
        item2 = ContextItem("i2", "v2", ContextSensitivity.PUBLIC_CONTEXT, "src", 0.0, base_m, ttl_seconds=10.0)

        snapshot = ContextSnapshot("snap", 0.0, base_m, items=(item1, item2))

        # At base_m + 4.0s: item1 expired, item2 fresh -> snapshot still active
        self.assertFalse(is_snapshot_expired(snapshot, current_monotonic=base_m + 4.0))
        unexpired = filter_unexpired_items(snapshot, current_monotonic=base_m + 4.0)
        self.assertEqual(len(unexpired), 1)
        self.assertEqual(unexpired[0].key, "i2")

        # At base_m + 11.0s: both expired -> snapshot is expired
        self.assertTrue(is_snapshot_expired(snapshot, current_monotonic=base_m + 11.0))

    def test_14_context_source_state_machine_valid_transitions(self):
        """14. Verifies valid lifecycle state transitions: UNINITIALIZED -> STARTING -> RUNNING -> STOPPING -> STOPPED."""
        source = MockContextSource("test_source")
        self.assertEqual(source.state, ContextSourceState.UNINITIALIZED)

        source._set_state(ContextSourceState.STARTING)
        self.assertEqual(source.state, ContextSourceState.STARTING)

        source._set_state(ContextSourceState.RUNNING)
        self.assertEqual(source.state, ContextSourceState.RUNNING)

        source._set_state(ContextSourceState.STOPPING)
        self.assertEqual(source.state, ContextSourceState.STOPPING)

        source._set_state(ContextSourceState.STOPPED)
        self.assertEqual(source.state, ContextSourceState.STOPPED)

    def test_15_context_source_invalid_transition_rejected(self):
        """15. Verifies that invalid state transitions raise InvalidStateTransitionError."""
        source = MockContextSource("test_source")
        self.assertEqual(source.state, ContextSourceState.UNINITIALIZED)

        with self.assertRaises(InvalidStateTransitionError):
            source._set_state(ContextSourceState.RUNNING)  # Must go through STARTING

        with self.assertRaises(InvalidStateTransitionError):
            source._set_state(ContextSourceState.STOPPING)

    def test_16_context_source_idempotent_start_stop(self):
        """16. Verifies that duplicate start and stop invocations are safely idempotent."""
        import asyncio

        source = MockContextSource("idempotent_source")
        asyncio.run(source.start())
        self.assertEqual(source.state, ContextSourceState.RUNNING)

        # Duplicate start
        asyncio.run(source.start())
        self.assertEqual(source.state, ContextSourceState.RUNNING)

        # Stop
        asyncio.run(source.stop())
        self.assertEqual(source.state, ContextSourceState.STOPPED)

        # Duplicate stop
        asyncio.run(source.stop())
        self.assertEqual(source.state, ContextSourceState.STOPPED)

    def test_17_mock_context_source_capture(self):
        """17. Verifies item capture from MockContextSource."""
        import asyncio

        source = MockContextSource("mock_cap")
        item = ContextItem("test.cap", "val", ContextSensitivity.PUBLIC_CONTEXT, "mock_cap", 0.0, 0.0)
        source.set_mock_items([item])

        asyncio.run(source.start())
        captured = asyncio.run(source.capture())
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].key, "test.cap")
        asyncio.run(source.stop())

    def test_18_context_manager_single_snapshot_replacement(self):
        """18. Verifies that ContextManager retains only the latest snapshot (zero history buffer)."""
        manager = ContextManager()
        self.assertIsNone(manager.get_current_snapshot())

        snap1 = ContextSnapshot("snap-1", 0.0, 0.0)
        manager.set_current_snapshot(snap1)
        self.assertEqual(manager.get_current_snapshot().snapshot_id, "snap-1")

        snap2 = ContextSnapshot("snap-2", 0.0, 0.0)
        manager.set_current_snapshot(snap2)
        # Snapshot 1 is completely replaced; no history list or ring buffer
        self.assertEqual(manager.get_current_snapshot().snapshot_id, "snap-2")
        self.assertFalse(hasattr(manager, "_history"))
        self.assertFalse(hasattr(manager, "_snapshots"))

    def test_19_context_manager_event_bus_dispatch(self):
        """19. Verifies that setting a snapshot publishes ContextSnapshotCreated on EventBus."""
        event_bus = EventBus()
        received_events = []

        def _handler(evt):
            received_events.append(evt)

        event_bus.subscribe(CONTEXT_SNAPSHOT_CREATED, _handler)
        manager = ContextManager(event_bus=event_bus)

        snap = ContextSnapshot("snap-event", 100.0, 200.0)
        manager.set_current_snapshot(snap)

        self.assertEqual(len(received_events), 1)
        self.assertEqual(received_events[0].event_type, CONTEXT_SNAPSHOT_CREATED)
        self.assertEqual(received_events[0].payload["snapshot_id"], "snap-event")

    def test_20_context_manager_zero_background_os_threads(self):
        """20. Verifies that ContextManager operates with zero background OS threads."""
        threads_before = threading.active_count()

        manager = ContextManager()
        snap = ContextSnapshot(
            snapshot_id="snap-thr",
            created_utc=time.time(),
            created_monotonic=time.monotonic(),
        )
        manager.set_current_snapshot(snap)
        _ = manager.get_current_snapshot()

        threads_after = threading.active_count()
        # Strictly zero background threads added by ContextManager
        self.assertEqual(threads_before, threads_after)


if __name__ == "__main__":
    unittest.main()
