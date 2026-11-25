"""Presence Process Manager Package
Provides asynchronous OS subprocess execution under Windows Job Object supervision.
"""

from runtime.process_manager.models import ProcessState, ManagedProcess
from runtime.process_manager.output_collector import ProcessOutputCollector
from runtime.process_manager.manager import ProcessManager

__all__ = [
    "ProcessState",
    "ManagedProcess",
    "ProcessOutputCollector",
    "ProcessManager",
]
