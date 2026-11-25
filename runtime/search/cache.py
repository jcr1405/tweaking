"""Search and Page Fetch Cache Subsystem
Provides normalized query hashing, configurable TTL expiration, negative result caching,
stale cache fallback on upstream outages, and conditional HTTP header tracking (ETag/Last-Modified).
"""

import re
import time
import string
import hashlib
import logging
import threading
from urllib.parse import urlparse, urlunparse
from dataclasses import dataclass
from typing import Optional, Dict, Any, Tuple, List, Callable

from runtime.search.models import SearchResponse, FetchResponse, ResponseClassification, SearchResult

logger = logging.getLogger("presence.search.cache")


def normalize_query(query: str) -> str:
    """Normalizes search query for stable caching across minor user syntax variations.
    Uses conservative normalization preserving language symbols (+, #, ., -) to avoid collisions
    between distinct terms like 'C', 'C++', and 'C#', or 'Node.js' and 'NodeJS'.
    """
    if not query:
        return ""
    q = query.strip()
    # Normalize multiple whitespace characters to single space
    q = re.sub(r"\s+", " ", q)
    # Strip peripheral quotes and enclosing brackets without stripping language tokens (+, #)
    q = q.strip("\"'“”`")
    # Lowercase
    q = q.lower()
    return q.strip()


def compute_query_hash(normalized_query: str, provider: str = "duckduckgo", max_results: int = 5) -> str:
    """Computes deterministic SHA-256 digest of normalized query and output parameters."""
    composite = f"{provider.lower()}:{max_results}:{normalized_query}"
    return hashlib.sha256(composite.encode("utf-8")).hexdigest()


def normalize_url(url: str) -> str:
    """Canonicalizes URL for stable cache lookups (strips fragments, lowercases scheme/host)."""
    parsed = urlparse(url.strip())
    # Normalizing scheme and netloc to lowercase, removing fragments
    canonical = (
        parsed.scheme.lower(),
        parsed.netloc.lower(),
        parsed.path or "/",
        parsed.params,
        parsed.query,
        ""  # drop fragment
    )
    return urlunparse(canonical)


@dataclass
class SearchCacheEntry:
    response: SearchResponse
    expires_at: float
    retrieved_at: str
    is_empty: bool


@dataclass
class FetchCacheEntry:
    response: FetchResponse
    expires_at: float
    retrieved_at: str
    etag: Optional[str] = None
    last_modified: Optional[str] = None


class SearchCache:
    """Thread-safe bounded memory cache for search queries and fetched web pages."""

    def __init__(
        self,
        search_ttl_seconds: float = 1800.0,
        empty_ttl_seconds: float = 300.0,
        fetch_ttl_seconds: float = 3600.0,
        max_entries: int = 1000,
        time_fn: Optional[Callable[[], float]] = None,
    ):
        self.search_ttl_seconds = search_ttl_seconds
        self.empty_ttl_seconds = empty_ttl_seconds
        self.fetch_ttl_seconds = fetch_ttl_seconds
        self.max_entries = max_entries
        self._time_fn = time_fn or time.time
        self._lock = threading.Lock()

        self._search_cache: Dict[str, SearchCacheEntry] = {}
        self._fetch_cache: Dict[str, FetchCacheEntry] = {}

        # Observability metrics
        self.search_hits: int = 0
        self.search_misses: int = 0
        self.search_stale_hits: int = 0
        self.fetch_hits: int = 0
        self.fetch_misses: int = 0
        self.fetch_stale_hits: int = 0

    def get_search(
        self,
        query: str,
        allow_stale: bool = False,
        provider: str = "duckduckgo",
        max_results: int = 5
    ) -> Optional[SearchResponse]:
        """Looks up a cached search query.

        Args:
            query: Raw or normalized query string.
            allow_stale: If True, returns expired entry marked as stale if present.
            provider: Search engine provider identifier.
            max_results: Maximum requested search results count.
        """
        norm = normalize_query(query)
        if not norm:
            return None

        q_hash = compute_query_hash(norm, provider=provider, max_results=max_results)
        now = self._time_fn()

        with self._lock:
            entry = self._search_cache.get(q_hash)
            if not entry:
                self.search_misses += 1
                return None

            # Fresh hit
            if now <= entry.expires_at:
                self.search_hits += 1
                cached_resp = SearchResponse(
                    classification=entry.response.classification,
                    results=[SearchResult(**r.to_dict()) for r in entry.response.results],
                    cached=True,
                    stale=False,
                    retrieved_at=entry.retrieved_at,
                    provider=entry.response.provider
                )
                return cached_resp

            # Stale hit
            if allow_stale:
                self.search_stale_hits += 1
                logger.info(f"Returning STALE cached search result for query '{norm[:30]}...' (retrieved {entry.retrieved_at})")
                stale_resp = SearchResponse(
                    classification=entry.response.classification,
                    results=[SearchResult(**r.to_dict()) for r in entry.response.results],
                    cached=True,
                    stale=True,
                    retrieved_at=entry.retrieved_at,
                    provider=entry.response.provider
                )
                return stale_resp

            self.search_misses += 1
            return None

    def put_search(
        self,
        query: str,
        response: SearchResponse,
        provider: str = "duckduckgo",
        max_results: int = 5
    ) -> None:
        """Caches a search response according to classification and policy."""
        # Never cache transport errors, challenges, rate-limits, or security blocks
        if response.classification not in (ResponseClassification.SUCCESS, ResponseClassification.EMPTY_RESULTS):
            logger.debug(f"Skipping cache for non-successful classification: {response.classification.value}")
            return

        norm = normalize_query(query)
        if not norm:
            return

        prov = response.provider or provider
        q_hash = compute_query_hash(norm, provider=prov, max_results=max_results)
        now = self._time_fn()
        is_empty = (response.classification == ResponseClassification.EMPTY_RESULTS or len(response.results) == 0)
        ttl = self.empty_ttl_seconds if is_empty else self.search_ttl_seconds

        with self._lock:
            if len(self._search_cache) >= self.max_entries:
                # Evict oldest entry
                oldest_key = min(self._search_cache.keys(), key=lambda k: self._search_cache[k].expires_at)
                del self._search_cache[oldest_key]

            retrieved_at = response.retrieved_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
            self._search_cache[q_hash] = SearchCacheEntry(
                response=response,
                expires_at=now + ttl,
                retrieved_at=retrieved_at,
                is_empty=is_empty
            )

    def get_fetch(self, url: str, allow_stale: bool = False) -> Optional[FetchResponse]:
        """Looks up a cached page fetch."""
        norm_url = normalize_url(url)
        now = self._time_fn()

        with self._lock:
            entry = self._fetch_cache.get(norm_url)
            if not entry:
                self.fetch_misses += 1
                return None

            if now <= entry.expires_at:
                self.fetch_hits += 1
                return FetchResponse(
                    classification=entry.response.classification,
                    url=entry.response.url,
                    content=entry.response.content,
                    content_type=entry.response.content_type,
                    status_code=entry.response.status_code,
                    cached=True,
                    stale=False,
                    retrieved_at=entry.retrieved_at,
                    etag=entry.etag,
                    last_modified=entry.last_modified
                )

            if allow_stale:
                self.fetch_stale_hits += 1
                return FetchResponse(
                    classification=entry.response.classification,
                    url=entry.response.url,
                    content=entry.response.content,
                    content_type=entry.response.content_type,
                    status_code=entry.response.status_code,
                    cached=True,
                    stale=True,
                    retrieved_at=entry.retrieved_at,
                    etag=entry.etag,
                    last_modified=entry.last_modified
                )

            self.fetch_misses += 1
            return None

    def put_fetch(
        self,
        url: str,
        response: FetchResponse,
        cache_control: Optional[str] = None,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
    ) -> None:
        """Caches a fetched page response respecting HTTP Cache-Control if present."""
        if response.classification != ResponseClassification.SUCCESS or not response.content:
            return

        # Obey Cache-Control: no-store / private
        ttl = self.fetch_ttl_seconds
        if cache_control:
            cc = cache_control.lower()
            if "no-store" in cc:
                logger.debug(f"Skipping cache for {url}: Cache-Control contains no-store")
                return
            # Parse max-age if present
            m = re.search(r"max-age=(\d+)", cc)
            if m:
                ttl = float(m.group(1))

        norm_url = normalize_url(url)
        now = self._time_fn()

        with self._lock:
            if len(self._fetch_cache) >= self.max_entries:
                oldest_key = min(self._fetch_cache.keys(), key=lambda k: self._fetch_cache[k].expires_at)
                del self._fetch_cache[oldest_key]

            retrieved_at = response.retrieved_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
            self._fetch_cache[norm_url] = FetchCacheEntry(
                response=response,
                expires_at=now + ttl,
                retrieved_at=retrieved_at,
                etag=etag,
                last_modified=last_modified
            )

    def invalidate_query(self, query: str) -> bool:
        """Removes a query from the cache."""
        norm = normalize_query(query)
        q_hash = compute_query_hash(norm)
        with self._lock:
            return self._search_cache.pop(q_hash, None) is not None

    def invalidate_url(self, url: str) -> bool:
        """Removes a URL from the fetch cache."""
        norm_url = normalize_url(url)
        with self._lock:
            return self._fetch_cache.pop(norm_url, None) is not None

    def clear(self) -> None:
        """Flushes all cached entries."""
        with self._lock:
            self._search_cache.clear()
            self._fetch_cache.clear()

    def get_metrics(self) -> Dict[str, Any]:
        """Returns diagnostic cache metrics."""
        with self._lock:
            return {
                "search_cached_entries": len(self._search_cache),
                "fetch_cached_entries": len(self._fetch_cache),
                "search_hits": self.search_hits,
                "search_misses": self.search_misses,
                "search_stale_hits": self.search_stale_hits,
                "fetch_hits": self.fetch_hits,
                "fetch_misses": self.fetch_misses,
                "fetch_stale_hits": self.fetch_stale_hits,
            }
