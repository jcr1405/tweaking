"""Search Traffic Governor and Anti-Burst Reliability Layer for Presence.
Provides centralized rate limiting, query deduplication, caching, circuit breaker protection,
and SSRF validation for web search and page-fetching capabilities.
"""

from runtime.search.models import (
    ResponseClassification,
    CircuitState,
    SearchResult,
    SearchResponse,
    FetchResponse,
    GovernorPolicy,
)
from runtime.search.governor import SearchGovernor, TurnSearchContext
from runtime.search.circuit_breaker import ProviderCircuitBreaker
from runtime.search.cache import SearchCache
from runtime.search.rate_limiter import ProviderRateLimiter
from runtime.search.deduplicator import InFlightDeduplicator
from runtime.search.ssrf import validate_destination_url

__all__ = [
    "SearchGovernor",
    "TurnSearchContext",
    "GovernorPolicy",
    "ResponseClassification",
    "CircuitState",
    "SearchResult",
    "SearchResponse",
    "FetchResponse",
    "ProviderCircuitBreaker",
    "SearchCache",
    "ProviderRateLimiter",
    "InFlightDeduplicator",
    "validate_destination_url",
]
