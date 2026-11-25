"""Provider and Domain Rate Limiter Subsystem
Enforces concurrent request limits, sliding-window frequency limits, inter-request intervals,
and bounded queue depths for search engines and web domains.
"""

import time
import logging
import threading
from urllib.parse import urlparse
from collections import deque
from typing import Optional, Dict, Any, Tuple, Callable

from runtime.search.models import GovernorPolicy

logger = logging.getLogger("presence.search.limiter")


class ProviderRateLimiter:
    """Thread-safe rate coordinator for external search providers and page fetching."""

    def __init__(
        self,
        policy: Optional[GovernorPolicy] = None,
        time_fn: Optional[Callable[[], float]] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
    ):
        self.policy = policy or GovernorPolicy()
        self._time_fn = time_fn or time.time
        self._sleep_fn = sleep_fn or time.sleep
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)

        # Provider (DuckDuckGo) state
        self._ddg_in_flight: int = 0
        self._ddg_queued: int = 0
        self._ddg_last_request_time: float = 0.0
        self._ddg_timestamps: deque = deque()  # sliding window of request start times

        # Fetch domain state
        self._fetch_global_in_flight: int = 0
        self._fetch_domain_in_flight: Dict[str, int] = {}
        self._fetch_domain_last_time: Dict[str, float] = {}
        self._fetch_timestamps: deque = deque()  # sliding window for global fetch rate limit

    def acquire_search_permit(self, timeout: float = 30.0) -> Tuple[bool, str]:
        """Blocks until a search request permit is available according to rate limits.

        Returns:
            Tuple of (acquired: bool, reason: str)
        """
        deadline = self._time_fn() + timeout
        start_mono = time.monotonic()

        with self._condition:
            # 1. Check queue bounds
            if self._ddg_queued >= self.policy.ddg_max_queued:
                logger.warning(
                    f"Search request rejected: Queue full ({self._ddg_queued}/{self.policy.ddg_max_queued} queued)."
                )
                return False, f"Search queue full ({self._ddg_queued} pending requests)."

            self._ddg_queued += 1

            try:
                while True:
                    now = self._time_fn()
                    if (time.monotonic() - start_mono) >= timeout or now >= deadline:
                        return False, "Search permit acquisition timed out in queue."

                    # Prune timestamps older than 60 seconds
                    cutoff = now - 60.0
                    while self._ddg_timestamps and self._ddg_timestamps[0] < cutoff:
                        self._ddg_timestamps.popleft()

                    # Check max per minute limit
                    if len(self._ddg_timestamps) >= self.policy.ddg_max_per_minute:
                        oldest = self._ddg_timestamps[0]
                        wait_for_window = max(0.05, (oldest + 60.0) - now)
                        if (now + wait_for_window) > deadline:
                            return False, f"Search request limit reached ({self.policy.ddg_max_per_minute}/min)."
                        self._condition.wait(timeout=wait_for_window)
                        continue

                    # Check concurrency limit
                    if self._ddg_in_flight >= self.policy.ddg_max_concurrent:
                        wait_time = min(0.2, max(0.05, deadline - now))
                        self._condition.wait(timeout=wait_time)
                        continue

                    # Check inter-request spacing interval (e.g. 5 seconds)
                    time_since_last = now - self._ddg_last_request_time
                    if time_since_last < self.policy.ddg_min_interval_seconds:
                        wait_interval = self.policy.ddg_min_interval_seconds - time_since_last
                        if (now + wait_interval) > deadline:
                            return False, f"Search rate limit: minimum {self.policy.ddg_min_interval_seconds}s spacing."
                        self._condition.wait(timeout=wait_interval)
                        continue

                    # All conditions met! Claim permit.
                    self._ddg_in_flight += 1
                    self._ddg_last_request_time = now
                    self._ddg_timestamps.append(now)
                    return True, "Permit granted."

            finally:
                self._ddg_queued -= 1

    def release_search_permit(self) -> None:
        """Releases an in-flight search permit."""
        with self._condition:
            if self._ddg_in_flight > 0:
                self._ddg_in_flight -= 1
            self._condition.notify_all()

    def acquire_fetch_permit(self, url: str, timeout: float = 30.0) -> Tuple[bool, str]:
        """Blocks until a page-fetch permit is available for the given domain."""
        parsed = urlparse(url)
        domain = (parsed.netloc or "unknown").lower()
        deadline = self._time_fn() + timeout
        start_mono = time.monotonic()

        with self._condition:
            while True:
                now = self._time_fn()
                if (time.monotonic() - start_mono) >= timeout or now >= deadline:
                    return False, f"Page fetch timed out waiting for permit for domain '{domain}'."

                # Prune global fetch timestamps older than 60 seconds
                cutoff = now - 60.0
                while self._fetch_timestamps and self._fetch_timestamps[0] < cutoff:
                    self._fetch_timestamps.popleft()

                # Check global sliding window limit (e.g. 10/min)
                if len(self._fetch_timestamps) >= self.policy.fetch_max_per_minute:
                    oldest = self._fetch_timestamps[0]
                    wait_for_window = max(0.05, (oldest + 60.0) - now)
                    if (now + wait_for_window) > deadline:
                        return False, f"Global page fetch rate limit reached ({self.policy.fetch_max_per_minute}/min)."
                    self._condition.wait(timeout=wait_for_window)
                    continue

                # Check global fetch concurrency (max 2)
                if self._fetch_global_in_flight >= self.policy.fetch_max_global_concurrent:
                    wait_time = min(0.2, max(0.05, deadline - now))
                    self._condition.wait(timeout=wait_time)
                    continue

                # Check per-domain concurrency (max 1)
                dom_in_flight = self._fetch_domain_in_flight.get(domain, 0)
                if dom_in_flight >= self.policy.fetch_max_per_domain_concurrent:
                    wait_time = min(0.2, max(0.05, deadline - now))
                    self._condition.wait(timeout=wait_time)
                    continue

                # Check minimum interval per domain (min 2 seconds)
                last_domain_time = self._fetch_domain_last_time.get(domain, 0.0)
                time_since_domain = now - last_domain_time
                if time_since_domain < self.policy.fetch_min_domain_interval_seconds:
                    wait_dom = self.policy.fetch_min_domain_interval_seconds - time_since_domain
                    if (now + wait_dom) > deadline:
                        return False, f"Per-domain rate limit for '{domain}' exceeded."
                    self._condition.wait(timeout=wait_dom)
                    continue

                # Permit acquired!
                self._fetch_global_in_flight += 1
                self._fetch_domain_in_flight[domain] = dom_in_flight + 1
                self._fetch_domain_last_time[domain] = now
                self._fetch_timestamps.append(now)
                return True, "Permit granted."

    def release_fetch_permit(self, url: str) -> None:
        """Releases an in-flight page-fetch permit."""
        parsed = urlparse(url)
        domain = (parsed.netloc or "unknown").lower()

        with self._condition:
            if self._fetch_global_in_flight > 0:
                self._fetch_global_in_flight -= 1
            if domain in self._fetch_domain_in_flight:
                self._fetch_domain_in_flight[domain] = max(0, self._fetch_domain_in_flight[domain] - 1)
                if self._fetch_domain_in_flight[domain] == 0:
                    del self._fetch_domain_in_flight[domain]
            self._condition.notify_all()

    def get_metrics(self) -> Dict[str, Any]:
        """Returns rate limiter metrics."""
        with self._condition:
            now = self._time_fn()
            cutoff = now - 60.0
            active_requests_in_min = sum(1 for t in self._ddg_timestamps if t >= cutoff)
            return {
                "search_in_flight": self._ddg_in_flight,
                "search_queued": self._ddg_queued,
                "search_requests_last_minute": active_requests_in_min,
                "fetch_global_in_flight": self._fetch_global_in_flight,
                "fetch_active_domains": len(self._fetch_domain_in_flight),
            }
