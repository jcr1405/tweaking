"""Search Traffic Governor and Central Coordinator
Orchestrates rate limiting, circuit breaker state, query deduplication, caching,
SSRF protection, and HTTP transport execution for all outgoing search and fetch traffic.
"""

import time
import httpx
import logging
import contextvars
from typing import Optional, Dict, Any, List, Callable
from urllib.parse import urlparse

from runtime.search.models import (
    ResponseClassification,
    CircuitState,
    SearchResult,
    SearchResponse,
    FetchResponse,
    GovernorPolicy,
)
from runtime.search.circuit_breaker import ProviderCircuitBreaker
from runtime.search.cache import SearchCache, normalize_query, normalize_url
from runtime.search.rate_limiter import ProviderRateLimiter
from runtime.search.deduplicator import InFlightDeduplicator
from runtime.search.ssrf import validate_destination_url, SSRFSafeTransport
from runtime.search.parser import DuckDuckGoParser, HtmlContentExtractor

logger = logging.getLogger("presence.search.governor")


class TurnSearchContext:
    """Tracks search and fetch request budgets per individual agent conversational turn."""

    def __init__(self, max_searches: Optional[int] = None, max_fetches: Optional[int] = None):
        self.max_searches = max_searches
        self.max_fetches = max_fetches
        self.searches_conducted: int = 0
        self.fetches_conducted: int = 0

    def can_search(self) -> bool:
        if self.max_searches is None:
            return True
        return self.searches_conducted < self.max_searches

    def record_search(self) -> None:
        self.searches_conducted += 1

    def can_fetch(self) -> bool:
        if self.max_fetches is None:
            return True
        return self.fetches_conducted < self.max_fetches

    def record_fetch(self) -> None:
        self.fetches_conducted += 1


active_turn_context: contextvars.ContextVar[Optional[TurnSearchContext]] = contextvars.ContextVar(
    "active_turn_context", default=None
)


class SearchGovernor:
    """Central authority and coordinator for all external search and web page fetch traffic."""

    def __init__(
        self,
        policy: Optional[GovernorPolicy] = None,
        time_fn: Optional[Callable[[], float]] = None,
        client: Optional[httpx.Client] = None,
    ):
        self.policy = policy or GovernorPolicy()
        self._time_fn = time_fn or time.time

        # Subsystems
        self.circuit_breaker = ProviderCircuitBreaker(
            provider_name="duckduckgo",
            cooldown_seconds=self.policy.ddg_circuit_cooldown_seconds,
            time_fn=self._time_fn
        )
        self.cache = SearchCache(
            search_ttl_seconds=self.policy.cache_search_ttl_seconds,
            empty_ttl_seconds=self.policy.cache_empty_ttl_seconds,
            fetch_ttl_seconds=self.policy.cache_fetch_ttl_seconds,
            max_entries=self.policy.cache_max_entries,
            time_fn=self._time_fn
        )
        self.rate_limiter = ProviderRateLimiter(
            policy=self.policy,
            time_fn=self._time_fn
        )
        self.deduplicator = InFlightDeduplicator()

        # Reusable HTTP client with explicit timeouts, connection limits, SSRFSafeTransport, and trust_env=False
        self._client = client or httpx.Client(
            transport=SSRFSafeTransport(
                verify=True,
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
                trust_env=False
            ),
            headers={
                "User-Agent": self.policy.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            },
            timeout=httpx.Timeout(self.policy.timeout_seconds, connect=5.0),
            follow_redirects=False,  # Redirects manually validated against SSRF
            trust_env=False,         # Ignore HTTP_PROXY / HTTPS_PROXY env vars
            verify=True,
        )
        self._owns_client = (client is None)

        # Operational telemetry
        self.total_search_requests: int = 0
        self.total_fetch_requests: int = 0
        self.successful_searches: int = 0
        self.failed_searches: int = 0
        self.successful_fetches: int = 0
        self.failed_fetches: int = 0
        self.last_error_category: Optional[str] = None

    def search(
        self,
        query: str,
        max_results: int = 5,
        turn_context: Optional[TurnSearchContext] = None,
        allow_stale_on_error: bool = True
    ) -> SearchResponse:
        """Executes a governed web search against DuckDuckGo."""
        self.total_search_requests += 1

        norm_q = normalize_query(query)
        if not norm_q:
            return SearchResponse(
                classification=ResponseClassification.EMPTY_RESULTS,
                results=[],
                error_message="Search query is empty or whitespace"
            )

        if turn_context is None:
            turn_context = active_turn_context.get()

        # 1. Enforce Per-Turn Search Budget
        if turn_context:
            if not turn_context.can_search():
                msg = f"Per-turn search budget exceeded (limit: {turn_context.max_searches} searches per turn)."
                logger.warning(msg)
                self.last_error_category = ResponseClassification.REQUEST_BUDGET_EXCEEDED.value
                return SearchResponse(
                    classification=ResponseClassification.REQUEST_BUDGET_EXCEEDED,
                    error_message=msg
                )

        # 2. Check Fresh Cache
        cached = self.cache.get_search(norm_q, allow_stale=False, provider="duckduckgo", max_results=max_results)
        if cached:
            logger.info(f"Search cache HIT for query: '{norm_q[:30]}...'")
            if turn_context:
                turn_context.record_search()
            return cached

        # 3. Check Provider Circuit Breaker
        allowed, cooldown_remaining, state = self.circuit_breaker.can_execute()
        if not allowed:
            # Check for stale cache fallback
            if allow_stale_on_error:
                stale = self.cache.get_search(norm_q, allow_stale=True, provider="duckduckgo", max_results=max_results)
                if stale:
                    logger.info(f"Circuit {state.value} - returning STALE cached search results for '{norm_q[:30]}...'")
                    if turn_context:
                        turn_context.record_search()
                    return stale

            msg = (
                f"DuckDuckGo search provider is currently unavailable (Circuit {state.value}). "
                f"Cooldown active: {cooldown_remaining:.0f}s remaining. Automated requests blocked."
            )
            logger.warning(msg)
            self.last_error_category = ResponseClassification.CIRCUIT_OPEN.value
            return SearchResponse(
                classification=ResponseClassification.CIRCUIT_OPEN,
                error_message=msg,
                retry_after=cooldown_remaining
            )

        # 4. In-Flight Request Deduplication
        is_leader, existing_result = self.deduplicator.join_or_lead_search(
            norm_q, provider="duckduckgo", max_results=max_results
        )
        if not is_leader and existing_result is not None:
            if turn_context:
                turn_context.record_search()
            return existing_result

        # 5. Acquire Rate Limiter Permit
        permit_ok, permit_reason = self.rate_limiter.acquire_search_permit(timeout=self.policy.timeout_seconds)
        if not permit_ok:
            self.deduplicator.complete_search(norm_q, None, provider="duckduckgo", max_results=max_results)
            self.last_error_category = ResponseClassification.RATE_LIMITED.value
            return SearchResponse(
                classification=ResponseClassification.RATE_LIMITED,
                error_message=f"Rate limit governor blocked search: {permit_reason}"
            )

        # 6. Execute Network Request with Bounded Retries
        response: Optional[SearchResponse] = None
        try:
            response = self._execute_ddg_http(norm_q, max_results)
        except Exception as e:
            logger.error(f"Unexpected search execution failure: {e}")
            response = SearchResponse(
                classification=ResponseClassification.NETWORK_ERROR,
                error_message=str(e)
            )
        finally:
            self.rate_limiter.release_search_permit()

        # 7. Post-Processing: Caching, Circuit Breaker & Fallback
        if response.is_success:
            self.successful_searches += 1
            self.circuit_breaker.record_success()
            self.cache.put_search(norm_q, response, provider="duckduckgo", max_results=max_results)
        else:
            self.failed_searches += 1
            self.last_error_category = response.classification.value
            self.circuit_breaker.record_failure(
                response.classification,
                reason=response.error_message or "",
                custom_cooldown=response.retry_after
            )

            # Stale fallback on transient errors
            if allow_stale_on_error and response.classification in (
                ResponseClassification.NETWORK_TIMEOUT,
                ResponseClassification.NETWORK_ERROR,
                ResponseClassification.UPSTREAM_SERVER_ERROR,
                ResponseClassification.RATE_LIMITED
            ):
                stale = self.cache.get_search(norm_q, allow_stale=True, provider="duckduckgo", max_results=max_results)
                if stale:
                    logger.info(f"Upstream search error ({response.classification.value}) - falling back to STALE cached result.")
                    response = stale

        if turn_context:
            turn_context.record_search()

        self.deduplicator.complete_search(
            norm_q, response, provider="duckduckgo", max_results=max_results
        )
        return response

    def _execute_ddg_http(self, query: str, max_results: int) -> SearchResponse:
        """Performs HTTP request against DuckDuckGo HTML endpoint with classification and safe retries."""
        url = "https://html.duckduckgo.com/html/"
        data = {"q": query, "b": ""}

        attempts = 0
        max_attempts = 2  # At most 1 retry for transient 502/503 or timeouts

        while attempts < max_attempts:
            attempts += 1
            try:
                resp = self._client.post(url, data=data)

                # HTTP 429: Rate Limited
                if resp.status_code == 429:
                    retry_after = self._parse_retry_after(resp.headers.get("Retry-After"))
                    return SearchResponse(
                        classification=ResponseClassification.RATE_LIMITED,
                        error_message="DuckDuckGo rate limit exceeded (HTTP 429).",
                        retry_after=retry_after
                    )

                # HTTP 403: Forbidden / Access Restriction
                if resp.status_code == 403:
                    return SearchResponse(
                        classification=ResponseClassification.ACCESS_DENIED,
                        error_message="DuckDuckGo access denied (HTTP 403). Automated requests restricted."
                    )

                # HTTP 502 / 503: Upstream Gateway / Service Error
                if resp.status_code in (502, 503):
                    if attempts < max_attempts:
                        logger.warning(f"DuckDuckGo returned HTTP {resp.status_code}. Retrying once after backoff...")
                        time.sleep(1.0)
                        continue
                    return SearchResponse(
                        classification=ResponseClassification.UPSTREAM_SERVER_ERROR,
                        error_message=f"DuckDuckGo server unavailable (HTTP {resp.status_code})."
                    )

                if resp.status_code != 200:
                    return SearchResponse(
                        classification=ResponseClassification.NETWORK_ERROR,
                        error_message=f"Unexpected HTTP status {resp.status_code} from DuckDuckGo."
                    )

                # Response payload checks
                html_body = resp.text[:self.policy.max_search_response_bytes]

                # Check for bot challenge content
                is_challenge, challenge_reason = DuckDuckGoParser.detect_challenge(html_body, resp.status_code)
                if is_challenge:
                    logger.warning(f"Bot challenge detected in DuckDuckGo response: {challenge_reason}")
                    return SearchResponse(
                        classification=ResponseClassification.PROVIDER_CHALLENGED,
                        error_message=f"DuckDuckGo challenge barrier detected ({challenge_reason}). Requests paused."
                    )

                # Check for empty results
                if DuckDuckGoParser.detect_empty_results(html_body):
                    return SearchResponse(
                        classification=ResponseClassification.EMPTY_RESULTS,
                        results=[]
                    )

                # Parse search results
                try:
                    results = DuckDuckGoParser.parse_results(html_body, max_results=max_results)
                except Exception as parse_ex:
                    logger.error(f"DuckDuckGo HTML parser exception: {parse_ex}")
                    return SearchResponse(
                        classification=ResponseClassification.PARSE_ERROR,
                        error_message=f"Parser exception: {parse_ex}"
                    )

                if not results:
                    logger.warning(
                        "DuckDuckGo returned 200 without recognized challenge or empty signatures, but parser extracted 0 results."
                    )
                    return SearchResponse(
                        classification=ResponseClassification.PARSE_ERROR,
                        error_message="Failed to parse search results from response HTML structure."
                    )

                now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._time_fn()))
                return SearchResponse(
                    classification=ResponseClassification.SUCCESS,
                    results=results,
                    retrieved_at=now_iso
                )

            except httpx.TimeoutException:
                if attempts < max_attempts:
                    logger.warning("DuckDuckGo request timed out. Retrying once...")
                    continue
                return SearchResponse(
                    classification=ResponseClassification.NETWORK_TIMEOUT,
                    error_message=f"DuckDuckGo search timed out after {self.policy.timeout_seconds}s."
                )
            except httpx.RequestError as req_err:
                return SearchResponse(
                    classification=ResponseClassification.NETWORK_ERROR,
                    error_message=f"Network transport error: {req_err}"
                )

        return SearchResponse(
            classification=ResponseClassification.NETWORK_ERROR,
            error_message="Search request exhausted retries without resolution."
        )

    def fetch_page(
        self,
        url: str,
        max_chars: int = 10000,
        turn_context: Optional[TurnSearchContext] = None,
        allow_stale_on_error: bool = True
    ) -> FetchResponse:
        """Fetches and extracts clean text from a web page with strict SSRF and redirect validation."""
        self.total_fetch_requests += 1

        if turn_context is None:
            turn_context = active_turn_context.get()

        # Enforce Per-Turn Page Fetch Budget (default: max 5 fetches per turn)
        if turn_context:
            if not turn_context.can_fetch():
                msg = f"Per-turn page fetch budget exceeded (limit: {turn_context.max_fetches} fetches per turn)."
                logger.warning(msg)
                self.last_error_category = ResponseClassification.REQUEST_BUDGET_EXCEEDED.value
                return FetchResponse(
                    classification=ResponseClassification.REQUEST_BUDGET_EXCEEDED,
                    url=url,
                    error_message=msg
                )

        # 1. Validate Initial URL against SSRF rules
        safe, reason = validate_destination_url(url)
        if not safe:
            logger.warning(f"SSRF rejection for URL '{url}': {reason}")
            self.failed_fetches += 1
            self.last_error_category = ResponseClassification.SSRF_BLOCKED.value
            return FetchResponse(
                classification=ResponseClassification.SSRF_BLOCKED,
                url=url,
                error_message=reason
            )

        norm_url = normalize_url(url)

        # 2. Check Fresh Cache
        cached = self.cache.get_fetch(norm_url, allow_stale=False)
        if cached:
            logger.info(f"Page fetch cache HIT for '{norm_url}'")
            if turn_context:
                turn_context.record_fetch()
            return cached

        # 3. In-Flight Request Deduplication
        is_leader, existing_result = self.deduplicator.join_or_lead_fetch(norm_url)
        if not is_leader and existing_result is not None:
            if turn_context:
                turn_context.record_fetch()
            return existing_result

        # 4. Acquire Domain Rate Limit Permit
        permit_ok, permit_reason = self.rate_limiter.acquire_fetch_permit(norm_url, timeout=self.policy.timeout_seconds)
        if not permit_ok:
            self.deduplicator.complete_fetch(norm_url, None)
            self.last_error_category = ResponseClassification.RATE_LIMITED.value
            return FetchResponse(
                classification=ResponseClassification.RATE_LIMITED,
                url=norm_url,
                error_message=permit_reason
            )

        # 5. Execute HTTP GET with manual redirect SSRF revalidation
        response: Optional[FetchResponse] = None
        try:
            response = self._execute_fetch_http(norm_url, max_chars)
        except Exception as e:
            logger.error(f"Unexpected page fetch failure for '{norm_url}': {e}")
            response = FetchResponse(
                classification=ResponseClassification.NETWORK_ERROR,
                url=norm_url,
                error_message=str(e)
            )
        finally:
            self.rate_limiter.release_fetch_permit(norm_url)

        # 6. Post-Processing & Caching
        if response.is_success:
            self.successful_fetches += 1
            self.cache.put_fetch(
                norm_url,
                response,
                etag=response.etag,
                last_modified=response.last_modified
            )
        else:
            self.failed_fetches += 1
            self.last_error_category = response.classification.value
            if allow_stale_on_error and response.classification in (
                ResponseClassification.NETWORK_TIMEOUT,
                ResponseClassification.NETWORK_ERROR,
                ResponseClassification.UPSTREAM_SERVER_ERROR
            ):
                stale = self.cache.get_fetch(norm_url, allow_stale=True)
                if stale:
                    logger.info(f"Page fetch error ({response.classification.value}) - falling back to STALE cached page.")
                    response = stale

        if turn_context:
            turn_context.record_fetch()

        self.deduplicator.complete_fetch(norm_url, response)
        return response

    def _execute_fetch_http(self, url: str, max_chars: int) -> FetchResponse:
        """Executes GET request with bounded response sizes and revalidated redirects."""
        current_url = url
        redirects = 0

        while redirects <= self.policy.max_redirects:
            try:
                resp = self._client.get(current_url)

                # Handle Redirects with strict SSRF revalidation
                if resp.is_redirect and "Location" in resp.headers:
                    redirects += 1
                    if redirects > self.policy.max_redirects:
                        return FetchResponse(
                            classification=ResponseClassification.NETWORK_ERROR,
                            url=url,
                            error_message=f"Maximum redirects ({self.policy.max_redirects}) exceeded."
                        )

                    location = resp.headers["Location"]
                    next_url = httpx.URL(current_url).join(location)
                    next_url_str = str(next_url)

                    # Revalidate redirect destination against SSRF!
                    safe, reason = validate_destination_url(next_url_str)
                    if not safe:
                        logger.warning(f"SSRF redirect blocked to '{next_url_str}': {reason}")
                        return FetchResponse(
                            classification=ResponseClassification.SSRF_BLOCKED,
                            url=url,
                            error_message=f"Redirect blocked: {reason}"
                        )

                    current_url = next_url_str
                    continue

                if resp.status_code == 429:
                    return FetchResponse(
                        classification=ResponseClassification.RATE_LIMITED,
                        url=url,
                        status_code=429,
                        error_message="Host rate limit exceeded (HTTP 429)."
                    )

                if resp.status_code in (401, 403):
                    return FetchResponse(
                        classification=ResponseClassification.ACCESS_DENIED,
                        url=url,
                        status_code=resp.status_code,
                        error_message=f"Access denied to page (HTTP {resp.status_code})."
                    )

                if resp.status_code >= 500:
                    return FetchResponse(
                        classification=ResponseClassification.UPSTREAM_SERVER_ERROR,
                        url=url,
                        status_code=resp.status_code,
                        error_message=f"Upstream server error (HTTP {resp.status_code})."
                    )

                if resp.status_code != 200:
                    return FetchResponse(
                        classification=ResponseClassification.NETWORK_ERROR,
                        url=url,
                        status_code=resp.status_code,
                        error_message=f"Failed to fetch page: HTTP {resp.status_code}."
                    )

                # Check Content-Type
                content_type = resp.headers.get("Content-Type", "")
                raw_text = resp.text[:self.policy.max_response_bytes]

                # Extract readable text
                if "html" in content_type.lower() or "<html" in raw_text[:500].lower():
                    clean_text = HtmlContentExtractor.extract_text(raw_text, max_chars=max_chars)
                else:
                    clean_text = raw_text[:max_chars]

                now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._time_fn()))
                return FetchResponse(
                    classification=ResponseClassification.SUCCESS,
                    url=url,
                    content=clean_text,
                    content_type=content_type,
                    status_code=200,
                    retrieved_at=now_iso,
                    etag=resp.headers.get("ETag"),
                    last_modified=resp.headers.get("Last-Modified")
                )

            except httpx.TimeoutException:
                return FetchResponse(
                    classification=ResponseClassification.NETWORK_TIMEOUT,
                    url=url,
                    error_message=f"Page fetch timed out after {self.policy.timeout_seconds}s."
                )
            except httpx.RequestError as req_err:
                return FetchResponse(
                    classification=ResponseClassification.NETWORK_ERROR,
                    url=url,
                    error_message=f"Network error fetching page: {req_err}"
                )

        return FetchResponse(
            classification=ResponseClassification.NETWORK_ERROR,
            url=url,
            error_message="Redirect processing failed."
        )

    def _parse_retry_after(self, val: Optional[str]) -> Optional[float]:
        """Safely parses HTTP Retry-After header into delay seconds."""
        if not val:
            return None
        try:
            return float(val.strip())
        except ValueError:
            return None

    def reset_circuit_breaker(self, provider: str = "duckduckgo") -> bool:
        """User-controlled circuit breaker reset."""
        if provider.lower() == "duckduckgo":
            self.circuit_breaker.reset()
            return True
        return False

    def get_status(self) -> Dict[str, Any]:
        """Observability interface for CLI and UI status monitoring."""
        cb_status = self.circuit_breaker.get_status()
        limiter_metrics = self.rate_limiter.get_metrics()
        cache_metrics = self.cache.get_metrics()

        return {
            "provider": cb_status["provider"],
            "circuit_state": cb_status["circuit_state"],
            "cooldown_remaining_seconds": cb_status["cooldown_remaining_seconds"],
            "search_in_flight": limiter_metrics["search_in_flight"],
            "search_queued": limiter_metrics["search_queued"],
            "search_requests_last_minute": limiter_metrics["search_requests_last_minute"],
            "total_searches": self.total_search_requests,
            "successful_searches": self.successful_searches,
            "failed_searches": self.failed_searches,
            "total_fetches": self.total_fetch_requests,
            "successful_fetches": self.successful_fetches,
            "failed_fetches": self.failed_fetches,
            "cache_hits": cache_metrics["search_hits"] + cache_metrics["fetch_hits"],
            "cache_misses": cache_metrics["search_misses"] + cache_metrics["fetch_misses"],
            "cache_entries": cache_metrics["search_cached_entries"] + cache_metrics["fetch_cached_entries"],
            "deduplicated_requests": self.deduplicator.deduplicated_searches_count + self.deduplicator.deduplicated_fetches_count,
            "last_error_category": self.last_error_category,
        }

    def close(self) -> None:
        """Gracefully closes HTTP client resources."""
        if self._owns_client and not self._client.is_closed:
            try:
                self._client.close()
            except Exception as e:
                logger.debug(f"Error closing HTTP client: {e}")
