"""Monotonic TTL Expiration Evaluator (Phase 2C.0)
Evaluates context item and snapshot expiration strictly against time.monotonic().
Guarantees zero background polling threads.
"""

import time
from typing import Optional, Tuple

from .models import ContextItem, ContextSnapshot


def is_item_expired(item: ContextItem, current_monotonic: Optional[float] = None) -> bool:
    """Evaluates whether an individual context item has exceeded its monotonic TTL.
    If item.ttl_seconds is None, the item does not expire by time.
    """
    if item.ttl_seconds is None:
        return False
    if current_monotonic is None:
        current_monotonic = time.monotonic()
    return (current_monotonic - item.observed_monotonic) > item.ttl_seconds


def is_snapshot_expired(snapshot: ContextSnapshot, current_monotonic: Optional[float] = None) -> bool:
    """Evaluates whether all items in a snapshot with a TTL have expired.
    An empty snapshot or one with only non-expiring items is not considered expired.
    """
    if current_monotonic is None:
        current_monotonic = time.monotonic()
    return snapshot.is_expired(current_monotonic)


def filter_unexpired_items(
    snapshot: ContextSnapshot, current_monotonic: Optional[float] = None
) -> Tuple[ContextItem, ...]:
    """Returns only items from the snapshot that have not exceeded their monotonic TTL."""
    if current_monotonic is None:
        current_monotonic = time.monotonic()
    return tuple(item for item in snapshot.items if not is_item_expired(item, current_monotonic))
