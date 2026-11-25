"""In-Flight Request Deduplication Subsystem
Coalesces concurrent identical search and fetch requests into a single network execution,
preventing redundant parallel requests for the same query or URL.
"""

import logging
import threading
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, Tuple

from runtime.search.cache import normalize_query, compute_query_hash, normalize_url

logger = logging.getLogger("presence.search.dedup")


@dataclass
class _InFlightSlot:
    event: threading.Event = field(default_factory=threading.Event)
    result: Optional[Any] = None
    error: Optional[Exception] = None
    waiters_count: int = 0


class InFlightDeduplicator:
    """Thread-safe coordinator for collapsing simultaneous identical requests."""

    def __init__(self):
        self._lock = threading.Lock()
        self._search_slots: Dict[str, _InFlightSlot] = {}
        self._fetch_slots: Dict[str, _InFlightSlot] = {}
        self.deduplicated_searches_count: int = 0
        self.deduplicated_fetches_count: int = 0

    def join_or_lead_search(
        self,
        query: str,
        provider: str = "duckduckgo",
        max_results: int = 5
    ) -> Tuple[bool, Optional[Any]]:
        """Determines if the caller is the leader (executes request) or waiter (joins existing).

        Returns:
            Tuple of (is_leader: bool, existing_result_or_none)
        """
        norm = normalize_query(query)
        q_hash = compute_query_hash(norm, provider=provider, max_results=max_results)

        with self._lock:
            if q_hash in self._search_slots:
                slot = self._search_slots[q_hash]
                slot.waiters_count += 1
                self.deduplicated_searches_count += 1
                logger.info(f"Coalescing duplicate concurrent search query for '{norm[:30]}...' (joined in-flight request)")
                is_leader = False
            else:
                slot = _InFlightSlot()
                self._search_slots[q_hash] = slot
                is_leader = True

        if is_leader:
            return True, None

        # Waiter path: wait for leader to complete execution
        slot.event.wait(timeout=35.0)
        if slot.error:
            raise slot.error
        return False, slot.result

    def complete_search(
        self,
        query: str,
        result: Any,
        error: Optional[Exception] = None,
        provider: str = "duckduckgo",
        max_results: int = 5
    ) -> None:
        """Leader completes the search request, broadcasting the result to all waiters."""
        norm = normalize_query(query)
        q_hash = compute_query_hash(norm, provider=provider, max_results=max_results)

        with self._lock:
            slot = self._search_slots.pop(q_hash, None)

        if slot:
            slot.result = result
            slot.error = error
            slot.event.set()

    def join_or_lead_fetch(self, url: str) -> Tuple[bool, Optional[Any]]:
        """Determines if the caller is the leader or waiter for a URL fetch."""
        norm_url = normalize_url(url)

        with self._lock:
            if norm_url in self._fetch_slots:
                slot = self._fetch_slots[norm_url]
                slot.waiters_count += 1
                self.deduplicated_fetches_count += 1
                logger.info(f"Coalescing duplicate concurrent fetch for URL '{norm_url}'")
                is_leader = False
            else:
                slot = _InFlightSlot()
                self._fetch_slots[norm_url] = slot
                is_leader = True

        if is_leader:
            return True, None

        slot.event.wait(timeout=35.0)
        if slot.error:
            raise slot.error
        return False, slot.result

    def complete_fetch(self, url: str, result: Any, error: Optional[Exception] = None) -> None:
        """Leader completes the fetch request, broadcasting the result to all waiters."""
        norm_url = normalize_url(url)

        with self._lock:
            slot = self._fetch_slots.pop(norm_url, None)

        if slot:
            slot.result = result
            slot.error = error
            slot.event.set()
