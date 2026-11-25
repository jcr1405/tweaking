"""Search Models and Classification Enums
Defines response classifications, circuit breaker states, data transfer objects,
and configurable safety policies for the Presence Search Traffic Governor.
"""

from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List


class ResponseClassification(str, Enum):
    """Categorized status of outgoing search or fetch operations."""
    SUCCESS = "SUCCESS"
    EMPTY_RESULTS = "EMPTY_RESULTS"
    RATE_LIMITED = "RATE_LIMITED"
    PROVIDER_CHALLENGED = "PROVIDER_CHALLENGED"
    ACCESS_DENIED = "ACCESS_DENIED"
    NETWORK_TIMEOUT = "NETWORK_TIMEOUT"
    NETWORK_ERROR = "NETWORK_ERROR"
    UPSTREAM_SERVER_ERROR = "UPSTREAM_SERVER_ERROR"
    PARSE_ERROR = "PARSE_ERROR"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    REQUEST_BUDGET_EXCEEDED = "REQUEST_BUDGET_EXCEEDED"
    SSRF_BLOCKED = "SSRF_BLOCKED"


class CircuitState(str, Enum):
    """Provider circuit breaker operational states."""
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


@dataclass
class SearchResult:
    """Individual normalized search result entry."""
    title: str
    url: str
    snippet: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet
        }


@dataclass
class SearchResponse:
    """Enriched response envelope from the search coordinator."""
    classification: ResponseClassification
    results: List[SearchResult] = field(default_factory=list)
    error_message: Optional[str] = None
    retry_after: Optional[float] = None
    cached: bool = False
    stale: bool = False
    retrieved_at: Optional[str] = None
    provider: str = "duckduckgo"

    @property
    def is_success(self) -> bool:
        return self.classification in (ResponseClassification.SUCCESS, ResponseClassification.EMPTY_RESULTS)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "status": "SUCCESS" if self.is_success else "ERROR",
            "classification": self.classification.value,
            "results": [r.to_dict() for r in self.results],
            "cached": self.cached,
            "stale": self.stale,
            "provider": self.provider
        }
        if self.retrieved_at:
            d["retrieved_at"] = self.retrieved_at
        if self.error_message:
            d["error"] = self.error_message
        if self.retry_after is not None:
            d["retry_after"] = self.retry_after
        return d


@dataclass
class FetchResponse:
    """Enriched response envelope from the page fetcher."""
    classification: ResponseClassification
    url: str
    content: Optional[str] = None
    content_type: Optional[str] = None
    status_code: Optional[int] = None
    error_message: Optional[str] = None
    cached: bool = False
    stale: bool = False
    retrieved_at: Optional[str] = None
    etag: Optional[str] = None
    last_modified: Optional[str] = None

    @property
    def is_success(self) -> bool:
        return self.classification == ResponseClassification.SUCCESS and self.content is not None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "status": "SUCCESS" if self.is_success else "ERROR",
            "classification": self.classification.value,
            "url": self.url,
            "cached": self.cached,
            "stale": self.stale
        }
        if self.content is not None:
            d["content"] = self.content
        if self.status_code is not None:
            d["status_code"] = self.status_code
        if self.retrieved_at:
            d["retrieved_at"] = self.retrieved_at
        if self.error_message:
            d["error"] = self.error_message
        if self.etag:
            d["etag"] = self.etag
        if self.last_modified:
            d["last_modified"] = self.last_modified
        return d


@dataclass
class GovernorPolicy:
    """Safety limits, rates, and caching policies for outgoing search traffic."""
    # DuckDuckGo search safety limits
    ddg_max_concurrent: int = 1
    ddg_min_interval_seconds: float = 5.0
    ddg_max_per_minute: int = 5
    ddg_max_per_turn: Optional[int] = None
    ddg_max_queued: int = 4
    ddg_circuit_cooldown_seconds: float = 1800.0  # 30 minutes

    # Page fetcher domain safety limits
    fetch_max_global_concurrent: int = 2
    fetch_max_per_domain_concurrent: int = 1
    fetch_min_domain_interval_seconds: float = 2.0
    fetch_max_per_turn: Optional[int] = None
    fetch_max_per_minute: int = 10

    # Cache TTL configurations
    cache_search_ttl_seconds: float = 1800.0       # 30 minutes
    cache_empty_ttl_seconds: float = 300.0         # 5 minutes
    cache_fetch_ttl_seconds: float = 3600.0        # 1 hour
    cache_max_entries: int = 1000

    # HTTP Client Configuration
    user_agent: str = "PresenceDesktop/1.0 (+https://github.com/presence-ai; desktop-client)"
    timeout_seconds: float = 10.0
    max_redirects: int = 5
    max_response_bytes: int = 2 * 1024 * 1024       # 2 MB limit for safety
    max_search_response_bytes: int = 500 * 1024    # 500 KB limit for search HTML
