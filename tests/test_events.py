"""Unit Tests for In-Memory Typed Event Bus
"""

import unittest
from runtime.events import EventBus, Event


class TestEventBus(unittest.TestCase):

    def setUp(self):
        self.bus = EventBus()

    def tearDown(self):
        self.bus.clear()

    def test_publish_and_subscribe(self):
        received_events = []

        def _handler(event: Event):
            received_events.append(event)

        self.bus.subscribe("TestEvent", _handler)
        self.bus.publish("TestEvent", {"key": "value"})

        self.assertEqual(len(received_events), 1)
        event = received_events[0]
        self.assertEqual(event.event_type, "TestEvent")
        self.assertEqual(event.payload["key"], "value")
        self.assertEqual(event.sequence_num, 1)

    def test_global_subscriber(self):
        received_all = []

        def _global_handler(event: Event):
            received_all.append(event.event_type)

        self.bus.subscribe_all(_global_handler)
        self.bus.publish("EventA")
        self.bus.publish("EventB")

        self.assertEqual(received_all, ["EventA", "EventB"])

    def test_error_isolation(self):
        """Tests that an unhandled exception in one subscriber does NOT halt other subscribers."""
        execution_trail = []

        def _faulty_handler(event: Event):
            execution_trail.append("faulty_start")
            raise RuntimeError("Simulated crash in handler")

        def _healthy_handler(event: Event):
            execution_trail.append("healthy_executed")

        self.bus.subscribe("RobustEvent", _faulty_handler)
        self.bus.subscribe("RobustEvent", _healthy_handler)

        # Should not raise exception
        self.bus.publish("RobustEvent", {"data": 123})

        self.assertIn("faulty_start", execution_trail)
        self.assertIn("healthy_executed", execution_trail)

    def test_unsubscribe(self):
        received = []

        def _handler(event: Event):
            received.append(event)

        self.bus.subscribe("EventX", _handler)
        self.bus.publish("EventX")
        self.assertEqual(len(received), 1)

        unsubscribed = self.bus.unsubscribe("EventX", _handler)
        self.assertTrue(unsubscribed)

        self.bus.publish("EventX")
        self.assertEqual(len(received), 1, "Handler must not be called after unsubscribe")


if __name__ == "__main__":
    unittest.main()
