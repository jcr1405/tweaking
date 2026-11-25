"""Search Traffic Governor and Reliability Test Suite
Comprehensive validation of rate limiting, circuit breaker states, caching,
in-flight deduplication, SSRF protection, and AgentExecutor turn integration.
"""

import time
import json
import unittest
from unittest.mock import MagicMock, patch
import httpx

from runtime.search.models import (
    ResponseClassification,
    CircuitState,
    SearchResult,
    SearchResponse,
    FetchResponse,
    GovernorPolicy,
)
from runtime.search.governor import SearchGovernor, TurnSearchContext, active_turn_context
from runtime.search.circuit_breaker import ProviderCircuitBreaker
from runtime.search.cache import SearchCache, normalize_query, normalize_url
from runtime.search.rate_limiter import ProviderRateLimiter
from runtime.search.deduplicator import InFlightDeduplicator
from runtime.search.ssrf import validate_destination_url
from runtime.search.parser import DuckDuckGoParser, HtmlContentExtractor
from runtime.tools.search_tools import build_search_tools
from runtime.permissions import PermissionBoundary, PermissionTier
from runtime.tools.registry import ToolRegistry


SAMPLE_DDG_HTML = """
<!DOCTYPE html>
<html>
<body>
<div class="result results_links results_links_deep web-result">
  <h2 class="result__title">
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2F&rut=1">Python Documentation</a>
  </h2>
  <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2F&rut=1">Official documentation for the Python programming language.</a>
  <div class="result__extras__url"><span class="result__url">https://docs.python.org/3/</span></div>
</div>
<div class="result results_links results_links_deep web-result">
  <h2 class="result__title">
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fpypi.org%2F&rut=1">PyPI · The Python Package Index</a>
  </h2>
  <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fpypi.org%2F&rut=1">Find, install and publish Python packages.</a>
  <div class="result__extras__url"><span class="result__url">https://pypi.org/</span></div>
</div>
</body>
</html>
"""

SAMPLE_EMPTY_HTML = """
<!DOCTYPE html>
<html>
<body>
<div class="no-results">No results found for query.</div>
</body>
</html>
"""

SAMPLE_CHALLENGE_HTML = """
<!DOCTYPE html>
<html>
<head><title>Bot Detection</title></head>
<body>
<div class="anomaly-detection">
  We have detected unusual traffic from your computer network. Please solve the CAPTCHA to proceed.
</div>
</body>
</html>
"""

SAMPLE_ARTICLE_HTML = """
<!DOCTYPE html>
<html>
<head><title>Sample Article</title></head>
<body>
<nav><a href="/">Home</a></nav>
<main>
  <h1>Introduction to Distributed Systems</h1>
  <p>Distributed systems coordinate components across multiple networked computers.</p>
</main>
<footer>Copyright 2026</footer>
</body>
</html>
"""


class ControllableClock:
    """Deterministic virtual clock for unit testing."""
    def __init__(self, start_time: float = 1000.0):
        self.current_time = start_time

    def time(self) -> float:
        return self.current_time

    def advance(self, seconds: float) -> None:
        self.current_time += seconds


class TestSearchTrafficGovernor(unittest.TestCase):
    """Exhaustive tests covering all 22 governor requirements."""

    def setUp(self):
        self.clock = ControllableClock(start_time=1000.0)
        self.policy = GovernorPolicy(
            ddg_max_concurrent=1,
            ddg_min_interval_seconds=5.0,
            ddg_max_per_minute=5,
            ddg_max_per_turn=3,
            ddg_max_queued=4,
            ddg_circuit_cooldown_seconds=1800.0,
            fetch_max_global_concurrent=2,
            fetch_max_per_domain_concurrent=1,
            fetch_min_domain_interval_seconds=2.0,
            cache_search_ttl_seconds=1800.0,
            cache_empty_ttl_seconds=300.0,
            cache_fetch_ttl_seconds=3600.0,
        )

    # 1. Sequential request spacing
    def test_01_sequential_request_spacing(self):
        limiter = ProviderRateLimiter(policy=self.policy, time_fn=self.clock.time)
        ok1, _ = limiter.acquire_search_permit(timeout=0.1)
        self.assertTrue(ok1)
        limiter.release_search_permit()

        # Immediately trying again without advancing time should be rejected or wait
        ok2, reason = limiter.acquire_search_permit(timeout=0.1)
        self.assertFalse(ok2)
        self.assertIn("spacing", reason.lower())

        # Advance past 5s spacing requirement
        self.clock.advance(5.1)
        ok3, _ = limiter.acquire_search_permit(timeout=0.1)
        self.assertTrue(ok3)
        limiter.release_search_permit()

    # 2. Concurrent request serialization
    def test_02_concurrent_request_serialization(self):
        limiter = ProviderRateLimiter(policy=self.policy, time_fn=self.clock.time)
        ok1, _ = limiter.acquire_search_permit(timeout=0.1)
        self.assertTrue(ok1)

        # Second concurrent permit cannot be acquired while first is held (max concurrent = 1)
        ok2, reason = limiter.acquire_search_permit(timeout=0.05)
        self.assertFalse(ok2)
        self.assertIn("timed out", reason.lower())

        limiter.release_search_permit()
        self.clock.advance(5.1)
        ok3, _ = limiter.acquire_search_permit(timeout=0.1)
        self.assertTrue(ok3)
        limiter.release_search_permit()

    # 3. Per-turn budget enforcement
    def test_03_per_turn_budget_enforcement(self):
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.post.return_value = httpx.Response(200, text=SAMPLE_DDG_HTML)

        gov = SearchGovernor(policy=self.policy, time_fn=self.clock.time, client=mock_client)
        turn_ctx = TurnSearchContext(max_searches=3)

        # First 3 searches should be allowed
        for i in range(3):
            self.clock.advance(6.0)
            res = gov.search(f"test query {i}", turn_context=turn_ctx)
            self.assertEqual(res.classification, ResponseClassification.SUCCESS)

        # 4th search in the same turn must be rejected with REQUEST_BUDGET_EXCEEDED
        self.clock.advance(6.0)
        res_exceeded = gov.search("test query 4", turn_context=turn_ctx)
        self.assertEqual(res_exceeded.classification, ResponseClassification.REQUEST_BUDGET_EXCEEDED)
        self.assertIn("budget exceeded", res_exceeded.error_message)
        # Verify network post was only called 3 times, not 4
        self.assertEqual(mock_client.post.call_count, 3)

    # 4. Provider-wide budget enforcement (max 5/min)
    def test_04_provider_wide_budget_enforcement(self):
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.post.return_value = httpx.Response(200, text=SAMPLE_DDG_HTML)

        gov = SearchGovernor(policy=self.policy, time_fn=self.clock.time, client=mock_client)

        for i in range(5):
            self.clock.advance(6.0)  # 6s * 5 = 30s (< 60s window)
            res = gov.search(f"query {i}")
            self.assertEqual(res.classification, ResponseClassification.SUCCESS)

        # 6th search within 60s should be rejected with RATE_LIMITED
        self.clock.advance(6.0)
        res6 = gov.search("query 6")
        self.assertEqual(res6.classification, ResponseClassification.RATE_LIMITED)
        self.assertIn("5/min", res6.error_message)

        # After sliding window elapses (60s), search should be allowed again
        self.clock.advance(60.0)
        res7 = gov.search("query 7")
        self.assertEqual(res7.classification, ResponseClassification.SUCCESS)

    # 5. Identical query cache hits
    def test_05_identical_query_cache_hits(self):
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.post.return_value = httpx.Response(200, text=SAMPLE_DDG_HTML)

        gov = SearchGovernor(policy=self.policy, time_fn=self.clock.time, client=mock_client)

        res1 = gov.search("  Python   docs  ")
        self.assertEqual(res1.classification, ResponseClassification.SUCCESS)
        self.assertFalse(res1.cached)
        self.assertEqual(mock_client.post.call_count, 1)

        # Second identical normalized query
        res2 = gov.search("python docs")
        self.assertEqual(res2.classification, ResponseClassification.SUCCESS)
        self.assertTrue(res2.cached)
        self.assertFalse(res2.stale)
        # Post was NOT called again
        self.assertEqual(mock_client.post.call_count, 1)

    # 6. Concurrent duplicate request sharing (in-flight deduplication)
    def test_06_concurrent_duplicate_request_sharing(self):
        dedup = InFlightDeduplicator()
        query = "async deduplication"

        is_leader_1, val_1 = dedup.join_or_lead_search(query)
        self.assertTrue(is_leader_1)
        self.assertIsNone(val_1)

        # In-flight duplicate arrives in a separate thread
        import threading
        results_thread = []

        def worker():
            is_leader_2, val_2 = dedup.join_or_lead_search(query)
            results_thread.append((is_leader_2, val_2))

        t = threading.Thread(target=worker)
        t.start()
        time.sleep(0.05)

        # Leader finishes and notifies
        expected_resp = SearchResponse(
            classification=ResponseClassification.SUCCESS,
            results=[SearchResult(title="Dedup", url="https://dedup.org", snippet="Dedup")]
        )
        dedup.complete_search(query, expected_resp)
        t.join(timeout=2.0)

        self.assertEqual(len(results_thread), 1)
        is_leader_thread, result_thread = results_thread[0]
        self.assertFalse(is_leader_thread)
        self.assertEqual(result_thread.results[0].title, "Dedup")
        self.assertEqual(dedup.deduplicated_searches_count, 1)

    # 7. Cache TTL expiration
    def test_07_cache_ttl_expiration(self):
        cache = SearchCache(search_ttl_seconds=1800.0, time_fn=self.clock.time)
        resp = SearchResponse(
            classification=ResponseClassification.SUCCESS,
            results=[SearchResult(title="Py", url="https://py.org", snippet="Py")]
        )
        cache.put_search("test query", resp)

        # Immediately: fresh hit
        hit = cache.get_search("test query", allow_stale=False)
        self.assertIsNotNone(hit)
        self.assertFalse(hit.stale)

        # Advance past 1800s TTL
        self.clock.advance(1801.0)
        fresh_hit = cache.get_search("test query", allow_stale=False)
        self.assertIsNone(fresh_hit)

        # But stale hit is available
        stale_hit = cache.get_search("test query", allow_stale=True)
        self.assertIsNotNone(stale_hit)
        self.assertTrue(stale_hit.stale)

    # 8. Stale-cache behavior on network failure
    def test_08_stale_cache_behavior_on_network_failure(self):
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.post.return_value = httpx.Response(200, text=SAMPLE_DDG_HTML)

        gov = SearchGovernor(policy=self.policy, time_fn=self.clock.time, client=mock_client)
        gov.search("python")

        # Advance past TTL
        self.clock.advance(1900.0)

        # Network fails on second request
        mock_client.post.side_effect = httpx.ConnectError("Network unreachable")
        res_stale = gov.search("python", allow_stale_on_error=True)

        self.assertEqual(res_stale.classification, ResponseClassification.SUCCESS)
        self.assertTrue(res_stale.stale)
        self.assertEqual(len(res_stale.results), 2)

    # 9. HTTP 429 with Retry-After header parsing
    def test_09_http_429_with_retry_after(self):
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.post.return_value = httpx.Response(429, headers={"Retry-After": "120"})

        gov = SearchGovernor(policy=self.policy, time_fn=self.clock.time, client=mock_client)
        res = gov.search("rate limited query", allow_stale_on_error=False)

        self.assertEqual(res.classification, ResponseClassification.RATE_LIMITED)
        self.assertEqual(res.retry_after, 120.0)

        # Circuit breaker should respect Retry-After
        allowed, cooldown_remaining, state = gov.circuit_breaker.can_execute()
        self.assertFalse(allowed)
        self.assertAlmostEqual(cooldown_remaining, 120.0, delta=1.0)

    # 10. HTTP 403 handling (access restriction)
    def test_10_http_403_access_restriction(self):
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.post.return_value = httpx.Response(403)

        gov = SearchGovernor(policy=self.policy, time_fn=self.clock.time, client=mock_client)
        res = gov.search("forbidden query", allow_stale_on_error=False)

        self.assertEqual(res.classification, ResponseClassification.ACCESS_DENIED)
        self.assertEqual(gov.circuit_breaker.state, CircuitState.OPEN)

    # 11. CAPTCHA / bot challenge content detection
    def test_11_captcha_detection(self):
        is_ch, reason = DuckDuckGoParser.detect_challenge(SAMPLE_CHALLENGE_HTML, status_code=200)
        self.assertTrue(is_ch)
        self.assertTrue("anomaly" in reason.lower() or "unusual traffic" in reason.lower())

        # Normal HTML is not a challenge
        is_ch_normal, _ = DuckDuckGoParser.detect_challenge(SAMPLE_DDG_HTML, status_code=200)
        self.assertFalse(is_ch_normal)

    # 12. Circuit breaker opening on challenge
    def test_12_circuit_breaker_opening_on_challenge(self):
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.post.return_value = httpx.Response(200, text=SAMPLE_CHALLENGE_HTML)

        gov = SearchGovernor(policy=self.policy, time_fn=self.clock.time, client=mock_client)
        res = gov.search("trigger challenge", allow_stale_on_error=False)

        self.assertEqual(res.classification, ResponseClassification.PROVIDER_CHALLENGED)
        self.assertEqual(gov.circuit_breaker.state, CircuitState.OPEN)

    # 13. Circuit breaker 30m cooldown enforcement
    def test_13_circuit_breaker_30m_cooldown(self):
        cb = ProviderCircuitBreaker(provider_name="duckduckgo", cooldown_seconds=1800.0, time_fn=self.clock.time)
        cb.record_failure(ResponseClassification.PROVIDER_CHALLENGED, "CAPTCHA detected")

        self.assertEqual(cb.state, CircuitState.OPEN)

        # At t + 600s: still OPEN
        self.clock.advance(600.0)
        allowed, remaining, state = cb.can_execute()
        self.assertFalse(allowed)
        self.assertEqual(state, CircuitState.OPEN)
        self.assertAlmostEqual(remaining, 1200.0, delta=1.0)

        # At t + 1801s: transitions to HALF_OPEN
        self.clock.advance(1201.0)
        allowed, remaining, state = cb.can_execute()
        self.assertTrue(allowed)
        self.assertEqual(state, CircuitState.HALF_OPEN)

    # 14. Half-open recovery probe success & failure reopening
    def test_14_half_open_recovery_probe(self):
        cb = ProviderCircuitBreaker(provider_name="duckduckgo", cooldown_seconds=1800.0, time_fn=self.clock.time)
        cb.record_failure(ResponseClassification.PROVIDER_CHALLENGED, "CAPTCHA")

        self.clock.advance(1801.0)
        # Probe allowed
        allowed, _, state = cb.can_execute()
        self.assertTrue(allowed)
        self.assertEqual(state, CircuitState.HALF_OPEN)

        # Second concurrent probe is BLOCKED while first probe in-flight
        allowed_2, _, state_2 = cb.can_execute()
        self.assertFalse(allowed_2)

        # Successful probe closes circuit
        cb.record_success()
        self.assertEqual(cb.state, CircuitState.CLOSED)

        # Now test probe failure re-opening circuit
        cb.record_failure(ResponseClassification.PROVIDER_CHALLENGED, "Another CAPTCHA")
        self.clock.advance(1801.0)
        cb.can_execute()  # Enters HALF_OPEN
        cb.record_failure(ResponseClassification.PROVIDER_CHALLENGED, "Failed probe")
        self.assertEqual(cb.state, CircuitState.OPEN)

    # 15. Network timeout handling & 1 controlled retry
    def test_15_network_timeout_and_single_retry(self):
        mock_client = MagicMock(spec=httpx.Client)
        # First call times out, second succeeds
        mock_client.post.side_effect = [
            httpx.TimeoutException("Read timeout"),
            httpx.Response(200, text=SAMPLE_DDG_HTML)
        ]

        gov = SearchGovernor(policy=self.policy, time_fn=self.clock.time, client=mock_client)
        res = gov.search("retry timeout query")

        self.assertEqual(res.classification, ResponseClassification.SUCCESS)
        self.assertEqual(mock_client.post.call_count, 2)

    # 16. Transient 502/503 handling & 1 controlled retry with backoff
    def test_16_transient_502_503_and_retry(self):
        mock_client = MagicMock(spec=httpx.Client)
        # First call 503, second call 200
        mock_client.post.side_effect = [
            httpx.Response(503),
            httpx.Response(200, text=SAMPLE_DDG_HTML)
        ]

        gov = SearchGovernor(policy=self.policy, time_fn=self.clock.time, client=mock_client)
        with patch("time.sleep"):  # skip actual sleep
            res = gov.search("retry 503 query")

        self.assertEqual(res.classification, ResponseClassification.SUCCESS)
        self.assertEqual(mock_client.post.call_count, 2)

    # 17. No retry for confirmed challenges
    def test_17_no_retry_for_confirmed_challenges(self):
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.post.return_value = httpx.Response(200, text=SAMPLE_CHALLENGE_HTML)

        gov = SearchGovernor(policy=self.policy, time_fn=self.clock.time, client=mock_client)
        res = gov.search("no retry on challenge", allow_stale_on_error=False)

        self.assertEqual(res.classification, ResponseClassification.PROVIDER_CHALLENGED)
        # Exactly 1 call was made — NEVER retry a confirmed challenge!
        self.assertEqual(mock_client.post.call_count, 1)

    # 18. Queue cancellation / timeout
    def test_18_queue_cancellation_and_timeout(self):
        limiter = ProviderRateLimiter(policy=self.policy, time_fn=self.clock.time)
        # Acquire the permit
        limiter.acquire_search_permit(timeout=0.1)

        # Saturated queue: attempt to acquire with immediate timeout
        ok, reason = limiter.acquire_search_permit(timeout=0.01)
        self.assertFalse(ok)
        self.assertIn("timed out", reason.lower())

    # 19. Clean shutdown with outstanding resources
    def test_19_clean_shutdown(self):
        gov = SearchGovernor(policy=self.policy)
        self.assertFalse(gov._client.is_closed)
        gov.close()
        self.assertTrue(gov._client.is_closed)

    # 20. Multiple agent sessions sharing provider-wide rate limits
    def test_20_multiple_agent_sessions_sharing_rate_limits(self):
        from runtime.session import AgentSessionFactory

        mock_client = MagicMock(spec=httpx.Client)
        mock_client.post.return_value = httpx.Response(200, text=SAMPLE_DDG_HTML)

        shared_gov = SearchGovernor(policy=self.policy, time_fn=self.clock.time, client=mock_client)
        factory = AgentSessionFactory(search_governor=shared_gov)

        sess_1 = factory.create_session(conversation_id="conv_shared_1")
        sess_2 = factory.create_session(conversation_id="conv_shared_2")

        # Session 1 tool catalog includes presence.web_search
        t1 = sess_1.tool_registry.get_tool("presence.web_search")
        t2 = sess_2.tool_registry.get_tool("presence.web_search")
        self.assertIsNotNone(t1)
        self.assertIsNotNone(t2)

        # Execute 5 searches across session 1 and session 2
        for i in range(3):
            self.clock.advance(6.0)
            res = t1.handler({"query": f"s1_q_{i}"})
            self.assertEqual(res["classification"], "SUCCESS")

        for i in range(2):
            self.clock.advance(6.0)
            res = t2.handler({"query": f"s2_q_{i}"})
            self.assertEqual(res["classification"], "SUCCESS")

        # 6th search across sessions must hit provider-wide limit (5/min)!
        self.clock.advance(6.0)
        res_blocked = t1.handler({"query": "s1_blocked"})
        self.assertEqual(res_blocked["classification"], "RATE_LIMITED")

        sess_1.close()
        sess_2.close()
        shared_gov.close()

    # 21. Separate domains maintaining separate fetch policies
    def test_21_separate_domains_fetch_policies(self):
        limiter = ProviderRateLimiter(policy=self.policy, time_fn=self.clock.time)

        # Acquire permit for example.com
        ok_a, _ = limiter.acquire_fetch_permit("https://example.com/page1", timeout=0.1)
        self.assertTrue(ok_a)

        # Concurrent request to DIFFERENT domain (python.org) succeeds!
        ok_b, _ = limiter.acquire_fetch_permit("https://python.org/page1", timeout=0.1)
        self.assertTrue(ok_b)

        # Concurrent request to SAME domain (example.com) is blocked!
        ok_a2, reason = limiter.acquire_fetch_permit("https://example.com/page2", timeout=0.05)
        self.assertFalse(ok_a2)
        self.assertIn("timed out", reason.lower())

        limiter.release_fetch_permit("https://example.com/page1")
        limiter.release_fetch_permit("https://python.org/page1")

    # 22. SSRF rejection and redirect validation
    def test_22_ssrf_rejection_and_redirect_validation(self):
        gov = SearchGovernor(policy=self.policy)

        # Direct localhost and private IP targets
        blocked_urls = [
            "http://localhost:8080/secret",
            "http://127.0.0.1/admin",
            "http://10.0.0.1/internal",
            "http://192.168.1.1/router",
            "http://169.254.169.254/latest/meta-data/",
            "ftp://example.com/file.txt",
            "file:///C:/Windows/System32/cmd.exe"
        ]

        for u in blocked_urls:
            resp = gov.fetch_page(u)
            self.assertEqual(resp.classification, ResponseClassification.SSRF_BLOCKED, f"Expected {u} to be SSRF blocked")

        # Revalidated redirect test: public URL redirects to private IP
        mock_client = MagicMock(spec=httpx.Client)
        # Step 1: 302 redirecting to http://127.0.0.1/evil
        mock_client.get.return_value = httpx.Response(
            302,
            headers={"Location": "http://127.0.0.1/evil"},
            request=httpx.Request("GET", "https://publicsite.com/redirect")
        )

        gov_redirect = SearchGovernor(policy=self.policy, client=mock_client)
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]):
            resp_redir = gov_redirect.fetch_page("https://publicsite.com/redirect")

        self.assertEqual(resp_redir.classification, ResponseClassification.SSRF_BLOCKED)
        self.assertIn("private or restricted IP", resp_redir.error_message)
        gov.close()
        gov_redirect.close()

    # 23. Test proving agent cannot produce additional requests once circuit is OPEN
    def test_23_agent_cannot_produce_requests_when_circuit_open(self):
        mock_client = MagicMock(spec=httpx.Client)
        gov = SearchGovernor(policy=self.policy, time_fn=self.clock.time, client=mock_client)

        # Trigger circuit OPEN via manual trip or challenge
        gov.circuit_breaker.record_failure(ResponseClassification.PROVIDER_CHALLENGED, "Challenge detected")
        self.assertEqual(gov.circuit_breaker.state, CircuitState.OPEN)

        # Subsequent attempts through tool handler
        search_tools = build_search_tools(governor=gov)
        web_search = next(t for t in search_tools if t.tool_id == "presence.web_search")

        res1 = web_search.handler({"query": "q1"})
        res2 = web_search.handler({"query": "q2"})
        res3 = web_search.handler({"query": "q3"})

        self.assertEqual(res1["classification"], "CIRCUIT_OPEN")
        self.assertEqual(res2["classification"], "CIRCUIT_OPEN")
        self.assertEqual(res3["classification"], "CIRCUIT_OPEN")

        # Mock HTTP client was NEVER called!
        self.assertEqual(mock_client.post.call_count, 0)
        gov.close()

    # 24. Punctuation semantic collisions (C vs C++ vs C#, Node.js vs NodeJS)
    def test_24_punctuation_semantic_no_collision(self):
        cache = SearchCache(search_ttl_seconds=1800.0, time_fn=self.clock.time)

        resp_c = SearchResponse(classification=ResponseClassification.SUCCESS, results=[SearchResult(title="C", url="https://c.org", snippet="C")])
        resp_cpp = SearchResponse(classification=ResponseClassification.SUCCESS, results=[SearchResult(title="C++", url="https://cpp.org", snippet="C++")])
        resp_csharp = SearchResponse(classification=ResponseClassification.SUCCESS, results=[SearchResult(title="C#", url="https://csharp.org", snippet="C#")])

        cache.put_search("C", resp_c)
        cache.put_search("C++", resp_cpp)
        cache.put_search("C#", resp_csharp)

        # Ensure C, C++, and C# return their own distinct responses without collision
        hit_c = cache.get_search("C")
        hit_cpp = cache.get_search("C++")
        hit_csharp = cache.get_search("C#")

        self.assertIsNotNone(hit_c)
        self.assertIsNotNone(hit_cpp)
        self.assertIsNotNone(hit_csharp)
        self.assertEqual(hit_c.results[0].title, "C")
        self.assertEqual(hit_cpp.results[0].title, "C++")
        self.assertEqual(hit_csharp.results[0].title, "C#")

        # Node.js vs NodeJS
        resp_nodejs = SearchResponse(classification=ResponseClassification.SUCCESS, results=[SearchResult(title="NodeJS", url="https://nodejs.org", snippet="NodeJS")])
        resp_node_dot_js = SearchResponse(classification=ResponseClassification.SUCCESS, results=[SearchResult(title="Node.js", url="https://nodejs.org/docs", snippet="Node.js")])

        cache.put_search("NodeJS", resp_nodejs)
        cache.put_search("Node.js", resp_node_dot_js)

        self.assertEqual(cache.get_search("NodeJS").results[0].title, "NodeJS")
        self.assertEqual(cache.get_search("Node.js").results[0].title, "Node.js")

        # Quoted queries normalize cleanly without stripping internal punctuation
        self.assertEqual(normalize_query('"python"'), "python")
        self.assertEqual(normalize_query("'C++'"), "c++")
        self.assertEqual(normalize_query("“Node.js”"), "node.js")

    # 25. Cache key accounts for max_results
    def test_25_cache_key_accounts_for_max_results(self):
        cache = SearchCache(search_ttl_seconds=1800.0, time_fn=self.clock.time)

        resp_3 = SearchResponse(classification=ResponseClassification.SUCCESS, results=[SearchResult(title=f"R{i}", url=f"https://r{i}.org", snippet="") for i in range(3)])
        resp_10 = SearchResponse(classification=ResponseClassification.SUCCESS, results=[SearchResult(title=f"R{i}", url=f"https://r{i}.org", snippet="") for i in range(10)])

        cache.put_search("python", resp_3, max_results=3)
        cache.put_search("python", resp_10, max_results=10)

        hit_3 = cache.get_search("python", max_results=3)
        hit_10 = cache.get_search("python", max_results=10)

        self.assertEqual(len(hit_3.results), 3)
        self.assertEqual(len(hit_10.results), 10)

    # 26. Page fetch turn budget enforcement
    def test_26_page_fetch_turn_budget_enforcement(self):
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.get.return_value = httpx.Response(200, text=SAMPLE_ARTICLE_HTML)

        gov = SearchGovernor(policy=self.policy, time_fn=self.clock.time, client=mock_client)
        turn_ctx = TurnSearchContext(max_fetches=5)

        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]):
            for i in range(5):
                self.clock.advance(2.5)  # advance past domain spacing interval
                res = gov.fetch_page(f"https://example.com/article_{i}", turn_context=turn_ctx)
                self.assertEqual(res.classification, ResponseClassification.SUCCESS)

            # 6th page fetch in the same turn must be rejected with REQUEST_BUDGET_EXCEEDED
            self.clock.advance(2.5)
            res_exceeded = gov.fetch_page("https://example.com/article_6", turn_context=turn_ctx)
            self.assertEqual(res_exceeded.classification, ResponseClassification.REQUEST_BUDGET_EXCEEDED)
            self.assertIn("budget exceeded", res_exceeded.error_message)
            self.assertEqual(mock_client.get.call_count, 5)

        gov.close()

    # 27. SSRF userinfo and encoded IP literal rejection
    def test_27_ssrf_userinfo_and_encoded_ip_rejection(self):
        gov = SearchGovernor(policy=self.policy)

        # Userinfo in URL
        r1 = gov.fetch_page("http://user:pass@example.com/secret")
        self.assertEqual(r1.classification, ResponseClassification.SSRF_BLOCKED)
        self.assertIn("Userinfo", r1.error_message)

        # Integer IP literal (127.0.0.1 = 2130706433)
        r2 = gov.fetch_page("http://2130706433/admin")
        self.assertEqual(r2.classification, ResponseClassification.SSRF_BLOCKED)

        # Hex IP literal (0x7f000001 = 127.0.0.1)
        r3 = gov.fetch_page("http://0x7f000001/admin")
        self.assertEqual(r3.classification, ResponseClassification.SSRF_BLOCKED)

        # Octal IP encoding (0177.0.0.1 = 127.0.0.1)
        r4 = gov.fetch_page("http://0177.0.0.1/admin")
        self.assertEqual(r4.classification, ResponseClassification.SSRF_BLOCKED)

        # 6to4 IP with embedded 127.0.0.1
        from runtime.search.ssrf import is_ip_disallowed
        import ipaddress
        ip_6to4_loopback = ipaddress.ip_address("2002:7f00:0001::")
        self.assertTrue(is_ip_disallowed(ip_6to4_loopback))

        gov.close()

    # 28. DNS rebinding socket protection (SSRFSafeBackend)
    def test_28_dns_rebinding_socket_protection(self):
        from runtime.search.ssrf import SSRFSafeBackend
        import httpcore

        backend = SSRFSafeBackend()
        # Mock socket peer IP returning 127.0.0.1
        mock_sock = MagicMock()
        mock_sock.getpeername.return_value = ("127.0.0.1", 80)

        mock_stream = MagicMock()
        mock_stream._sock = mock_sock

        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 80))]):
            with patch("httpcore.SyncBackend.connect_tcp", return_value=mock_stream):
                with self.assertRaises(httpcore.ConnectError) as cm:
                    backend.connect_tcp("rebind.attacker.com", 80)
                self.assertIn("DNS rebinding", str(cm.exception))
                mock_sock.close.assert_called_once()

    # 29. Parser distinguishes empty from parse error
    def test_29_parser_distinguishes_empty_from_parse_error(self):
        mock_client = MagicMock(spec=httpx.Client)
        # HTML with 200 OK that has no recognized empty signatures and no results (DOM changed)
        corrupted_html = "<html><body><div>Welcome to DuckDuckGo, search bar here.</div></body></html>"
        mock_client.post.return_value = httpx.Response(200, text=corrupted_html)

        gov = SearchGovernor(policy=self.policy, time_fn=self.clock.time, client=mock_client)
        res = gov.search("query on changed dom", allow_stale_on_error=False)

        self.assertEqual(res.classification, ResponseClassification.PARSE_ERROR)
        self.assertIn("Failed to parse", res.error_message)

        # But recognized empty results HTML returns EMPTY_RESULTS
        self.clock.advance(6.0)  # advance past DDG 5s spacing requirement
        mock_client.post.return_value = httpx.Response(200, text=SAMPLE_EMPTY_HTML)
        res_empty = gov.search("legit empty query", allow_stale_on_error=False)
        self.assertEqual(res_empty.classification, ResponseClassification.EMPTY_RESULTS)
        gov.close()

    # 30. Proxy environment isolation
    def test_30_proxy_env_isolation(self):
        gov = SearchGovernor(policy=self.policy)
        # Verify default client has trust_env=False
        self.assertFalse(gov._client.trust_env)
        gov.close()


if __name__ == "__main__":
    unittest.main()
