"""Multi-Provider Coordinator & Resilient Local Failover Subsystem
Provides dynamic, request-scoped multi-provider inference dispatch for Presence.
Eliminates single-provider failure behavior by automatically falling back from
exhausted cloud providers (e.g. FreeLLMAPI, Gemini) to local Qwen 3.5 9B (Ollama)
without losing conversation context, repeating completed tool operations, or restarting sessions.
Thread-safe and concurrency-isolated across multiple simultaneous AgentSessions.
"""

import time
import json
import logging
import threading
from datetime import datetime, timezone, timedelta
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List, Iterator, Tuple

from runtime.providers.base import IModelProvider, ModelStreamChunk

logger = logging.getLogger("presence.providers.coordinator")


class FailureCategory(str, Enum):
    """Normalized provider failure categories for safe fault isolation."""
    RATE_LIMIT = "RATE_LIMIT"                       # 429, temporary burst limits, too many requests
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"             # Confirmed daily/monthly quota exhaustion
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"   # 502, 503, 504, connect/timeout/DNS error
    AUTH_CONFIG = "AUTH_CONFIG"                     # 401, 403, missing API key, invalid credentials
    INVALID_REQUEST = "INVALID_REQUEST"             # 400, schema validation failure, malformed payload
    CONTEXT_LIMIT = "CONTEXT_LIMIT"                 # 413, context length exceeded, token budget overflow
    UNKNOWN = "UNKNOWN"                             # Unclassified provider exception


@dataclass(frozen=True)
class InferenceRequestConfig:
    """Immutable, request-scoped inference configuration.
    Guarantees thread-safety and prevents state leakage across concurrent sessions.
    """
    provider: Optional[str] = None                  # Explicit provider override ("ollama", "freellmapi", "gemini")
    model: Optional[str] = None                     # Explicit model ID override
    mode: str = "routine"                           # "routine" (Mode A: think=False) or "reasoning" (Mode B: think=True)
    purpose: str = "tool_execution"                 # "tool_execution", "task_planning", "conversation", "diagnostic"
    max_output_tokens: int = 4096
    timeout_seconds: float = 60.0
    fallback_policy: str = "local_eligible"          # "local_eligible", "cloud_only", "no_fallback", "all"
    cancellation_token: Optional[Any] = None
    task_complexity: Optional[str] = None           # Complexity from TDEL (e.g. "SIMPLE", "MODERATE", "COMPLEX")
    conversation_id: Optional[str] = None           # Conversation UUID for session-isolated failover tracking


def classify_provider_error(err: Exception) -> Tuple[FailureCategory, str, Optional[float]]:
    """Analyzes a provider exception and extracts category, sanitized reason, and optional retry-after seconds.
    Accurately distinguishes between temporary burst 429s, daily/monthly quota exhaustions, and upstream outages.
    """
    err_str = str(err).lower()
    retry_after: Optional[float] = None

    # Check for explicit retry-after patterns in error text (e.g. Retry-After: 120)
    if "retry-after" in err_str or "retry after" in err_str:
        import re
        m = re.search(r"retry[- ]after[:\s]+(\d+)", err_str)
        if m:
            try:
                retry_after = float(m.group(1))
            except ValueError:
                pass

    # 1. Confirmed Daily or Monthly Quota Exhaustion
    quota_exhausted_patterns = [
        "daily_quota_exhausted",
        "allocation of 10,000 neurons",
        "daily free allocation",
        "daily quota",
        "daily limit",
        "monthly quota",
        "monthly allocation",
        "monthly limit",
        "quota_exhausted",
        "neurons, please upgrade",
    ]
    if any(p in err_str for p in quota_exhausted_patterns):
        if retry_after is None:
            # Calculate remaining seconds until next midnight UTC (daily reset)
            now_utc = datetime.now(timezone.utc)
            midnight_utc = (now_utc + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            secs_to_midnight = (midnight_utc - now_utc).total_seconds()
            retry_after = max(3600.0, secs_to_midnight)
        return FailureCategory.QUOTA_EXHAUSTED, str(err), retry_after

    # 2. Temporary Rate Limit / Burst Limit (HTTP 429 without daily/monthly exhaustion)
    rate_limit_patterns = [
        "429",
        "rate limit",
        "too many requests",
        "resource_exhausted",
        "quota exceeded",
    ]
    if any(p in err_str for p in rate_limit_patterns):
        return FailureCategory.RATE_LIMIT, str(err), retry_after

    # 3. Upstream Availability / Gateway Errors
    upstream_patterns = [
        "502",
        "503",
        "504",
        "bad gateway",
        "upstream_error",
        "upstream_failed",
        "service unavailable",
        "gateway timeout",
        "connecterror",
        "connection refused",
        "connection reset",
        "timed out",
        "timeout",
        "network unreachable",
        "temporary failure",
        "cannot connect to",
    ]
    if any(p in err_str for p in upstream_patterns):
        return FailureCategory.UPSTREAM_UNAVAILABLE, str(err), retry_after

    # 4. Authentication & Configuration
    auth_patterns = [
        "401",
        "403",
        "unauthorized",
        "forbidden",
        "authentication failed",
        "invalid api key",
        "check your api key",
        "not configured",
        "permission denied",
    ]
    if any(p in err_str for p in auth_patterns):
        return FailureCategory.AUTH_CONFIG, str(err), retry_after

    # 5. Context or Token Limits
    context_patterns = [
        "413",
        "context length",
        "maximum context",
        "prompt too large",
        "too long",
        "token limit",
    ]
    if any(p in err_str for p in context_patterns):
        return FailureCategory.CONTEXT_LIMIT, str(err), retry_after

    # 6. Invalid Request / Schema
    invalid_patterns = [
        "400",
        "invalid_request",
        "schema validation",
        "malformed",
        "unsupported parameter",
    ]
    if any(p in err_str for p in invalid_patterns):
        return FailureCategory.INVALID_REQUEST, str(err), retry_after

    return FailureCategory.UNKNOWN, str(err), retry_after


class ProviderCircuit:
    """Thread-safe circuit breaker and cooldown tracking for an individual provider."""

    def __init__(self, provider_name: str):
        self.provider_name = provider_name
        self._lock = threading.Lock()
        self.failure_count = 0
        self.success_count = 0
        self.cooldown_until: float = 0.0
        self.last_failure_reason: str = ""
        self.last_failure_category: Optional[FailureCategory] = None

    def trip(self, cooldown_seconds: float, reason: str, category: FailureCategory) -> None:
        """Opens circuit cooldown for the specified duration."""
        with self._lock:
            self.failure_count += 1
            if cooldown_seconds <= 0.0:
                self.cooldown_until = 0.0
            else:
                self.cooldown_until = time.time() + cooldown_seconds
            self.last_failure_reason = reason
            self.last_failure_category = category
            logger.warning(
                f"Provider '{self.provider_name}' circuit tripped for {cooldown_seconds:.1f}s. "
                f"Reason: {reason[:100]} | Category: {category.value}"
            )

    def record_success(self) -> None:
        """Records a successful response and resets failure count."""
        with self._lock:
            self.success_count += 1
            self.failure_count = 0
            self.cooldown_until = 0.0

    def is_in_cooldown(self) -> Tuple[bool, float]:
        """Returns (in_cooldown, remaining_seconds)."""
        with self._lock:
            now = time.time()
            if now < self.cooldown_until:
                return True, self.cooldown_until - now
            return False, 0.0

    def reset(self) -> None:
        """Manually clears cooldown."""
        with self._lock:
            self.cooldown_until = 0.0
            self.failure_count = 0


def is_fallback_eligible(
    config: InferenceRequestConfig,
    category: FailureCategory,
    failed_provider: str,
    target_provider: str
) -> Tuple[bool, str]:
    """Evaluates whether an active inference request is eligible to fall back to target_provider."""
    from runtime.config import is_local_only_mode, is_cloud_provider
    if is_local_only_mode():
        if is_cloud_provider(target_provider):
            return False, f"Fallback to cloud provider '{target_provider}' is strictly forbidden in local-only mode."
        # In local-only mode, Ollama is the primary engine for all task complexities
        if target_provider == "ollama":
            return True, "Eligible for local Ollama execution."

    # Policy checks
    if config.fallback_policy == "no_fallback":
        return False, "Fallback disabled by request policy ('no_fallback')."

    if config.fallback_policy == "cloud_only" and target_provider == "ollama":
        return False, "Request policy explicitly requires cloud inference only ('cloud_only')."

    # Non-recoverable failure types (unless falling back from failed cloud auth to local)
    if category == FailureCategory.INVALID_REQUEST:
        return False, f"Provider error '{category.value}' represents an invalid request schema and cannot be resolved by provider failover."

    if category == FailureCategory.AUTH_CONFIG:
        if failed_provider != "ollama" and target_provider == "ollama":
            return True, "Cloud authentication failed; eligible for local fallback."
        return False, f"Authentication failure on '{failed_provider}' cannot be retried on '{target_provider}'."

    # Local fallback complexity gating (Part 4)
    if target_provider == "ollama":
        if config.task_complexity in ("COMPLEX", "ARCHITECTURAL"):
            return False, (
                f"Task complexity '{config.task_complexity}' requires cloud model reasoning and "
                f"is not eligible for silent local degradation."
            )
        ineligible_purposes = {
            "deep_architectural_reasoning",
            "complex_architectural",
            "research_heavy",
            "cloud_contract_planning",
        }
        if config.purpose in ineligible_purposes:
            return False, f"Request purpose '{config.purpose}' is not eligible for local fallback."

    return True, "Eligible for fallback."


class MultiProviderCoordinator(IModelProvider):
    """Dynamic Multi-Provider Coordinator implementing the unified IModelProvider interface.
    
    Coordinates inference requests across Ollama, FreeLLMAPI, and Gemini. Automatically detects
    upstream rate-limits (HTTP 429) or gateway failures (HTTP 502/503), isolates failures,
    and transparently falls back to local Qwen 3.5 9B with zero loss of conversation state or
    re-execution of side-effects.
    
    Thread-safe and session-isolated: multiple simultaneous AgentSessions sharing the same coordinator
    retain their own independent turn-local provider states without cross-session pollution.
    """

    def __init__(
        self,
        providers: Dict[str, IModelProvider],
        primary_provider: str = "freellmapi",
        fallback_order: Optional[List[str]] = None,
        event_bus: Optional[Any] = None,
        conversation_id: Optional[str] = None,
        rate_limit_cooldown_seconds: float = 60.0,
        upstream_cooldown_seconds: float = 15.0,
    ):
        from runtime.config import is_local_only_mode, is_cloud_provider
        self._lock = threading.RLock()

        if is_local_only_mode():
            # In local-only mode, cloud providers are strictly excluded
            filtered_providers = {k: v for k, v in providers.items() if not is_cloud_provider(k)}
            self._providers: Dict[str, IModelProvider] = filtered_providers
            self._primary_provider = "ollama" if "ollama" in filtered_providers else (next(iter(filtered_providers)) if filtered_providers else "none")
            self._fallback_order: List[str] = [p for p in (fallback_order or []) if not is_cloud_provider(p)]
        else:
            self._providers: Dict[str, IModelProvider] = dict(providers)
            self._primary_provider: str = primary_provider
            self._fallback_order: List[str] = fallback_order or ["ollama", "gemini"]

        self._event_bus = event_bus
        self._conversation_id = conversation_id or ""
        self._rate_limit_cooldown = rate_limit_cooldown_seconds
        self._upstream_cooldown = upstream_cooldown_seconds

        # Per-provider circuit breakers
        self._circuits: Dict[str, ProviderCircuit] = {
            p_name: ProviderCircuit(p_name) for p_name in self._providers
        }

        # Session-isolated turn-local active providers: conversation_id -> active_provider
        self._session_active_providers: Dict[str, str] = {}

    @property
    def provider_name(self) -> str:
        active = self.active_provider_name
        return f"coordinator[{active}]"

    @property
    def model_id(self) -> str:
        active_p = self.get_provider(self.active_provider_name)
        if active_p:
            return active_p.model_id
        return "multi-provider-coordinator"

    @property
    def primary_provider_name(self) -> str:
        with self._lock:
            return self._primary_provider

    @property
    def active_provider_name(self) -> str:
        """Returns the currently active provider for the default conversation, or primary."""
        return self.get_active_provider_for_conversation(self._conversation_id)

    def get_active_provider_for_conversation(self, conversation_id: str = "") -> str:
        """Thread-safe lookup of active provider for a specific conversation."""
        with self._lock:
            cid = conversation_id or self._conversation_id or ""
            if cid and cid in self._session_active_providers:
                active = self._session_active_providers[cid]
                if active in self._providers:
                    return active
            return self._primary_provider

    def set_primary_provider(self, name: str) -> None:
        """Updates the configured primary provider."""
        from runtime.config import is_local_only_mode, is_cloud_provider
        if is_local_only_mode() and is_cloud_provider(name):
            raise ValueError(f"Cannot set cloud provider '{name}' as primary in local-only mode.")
        with self._lock:
            if name in self._providers:
                self._primary_provider = name
                logger.info(f"Primary inference provider set to '{name}'.")

    def register_provider(self, name: str, provider: IModelProvider) -> None:
        """Registers or replaces an inference provider instance."""
        from runtime.config import is_local_only_mode, is_cloud_provider
        if is_local_only_mode() and is_cloud_provider(name):
            logger.warning(f"Registration of cloud provider '{name}' rejected: Local-only mode is active.")
            raise ValueError(f"Registration of cloud provider '{name}' rejected: Local-only mode is active.")
        with self._lock:
            self._providers[name] = provider
            if name not in self._circuits:
                self._circuits[name] = ProviderCircuit(name)

    def get_provider(self, name: str) -> Optional[IModelProvider]:
        with self._lock:
            return self._providers.get(name)

    def get_circuit_status(self, name: str) -> Dict[str, Any]:
        with self._lock:
            c = self._circuits.get(name)
        if not c:
            return {"provider": name, "status": "UNKNOWN"}
        in_cd, rem = c.is_in_cooldown()
        return {
            "provider": name,
            "in_cooldown": in_cd,
            "cooldown_remaining_seconds": rem,
            "failures": c.failure_count,
            "successes": c.success_count,
            "last_failure_reason": c.last_failure_reason,
            "last_failure_category": c.last_failure_category.value if c.last_failure_category else None
        }

    def validate_configuration(self) -> Tuple[bool, str]:
        """Validates that at least one registered provider is valid."""
        with self._lock:
            providers_copy = dict(self._providers)
        if not providers_copy:
            return False, "No model providers are registered in MultiProviderCoordinator."
        
        valid_providers = []
        messages = []
        for name, prov in providers_copy.items():
            valid, msg = prov.validate_configuration()
            if valid:
                valid_providers.append(name)
            messages.append(f"[{name}: {'OK' if valid else 'FAIL'} - {msg}]")

        if valid_providers:
            return True, f"Coordinator operational. Active: '{self.active_provider_name}'. Available: {', '.join(valid_providers)}. " + "; ".join(messages)
        return False, "All registered model providers failed configuration check: " + "; ".join(messages)

    def start_turn(self, conversation_id: str = "", primary_provider: Optional[str] = None) -> None:
        """Initializes turn-local execution state isolated per conversation."""
        with self._lock:
            cid = conversation_id or self._conversation_id or ""
            if cid in self._session_active_providers:
                del self._session_active_providers[cid]

    def end_turn(self, conversation_id: str = "") -> None:
        """Cleans up turn-local execution state for the specified conversation."""
        with self._lock:
            cid = conversation_id or self._conversation_id or ""
            self._session_active_providers.pop(cid, None)

    def _emit_event(self, event_type: str, payload: Dict[str, Any], conversation_id: str = "") -> None:
        """Emits a typed event to the EventBus if configured."""
        if self._event_bus:
            try:
                base_payload = {
                    "conversation_id": conversation_id or self._conversation_id,
                    "timestamp": time.time(),
                }
                base_payload.update(payload)
                self._event_bus.publish(event_type, base_payload)
            except Exception as e:
                logger.debug(f"Failed to publish event '{event_type}': {e}")

    def generate_turn(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: str = "",
        tools: Optional[List[Dict[str, Any]]] = None,
        cancellation_token: Optional[Any] = None,
        request_config: Optional[InferenceRequestConfig] = None
    ) -> str:
        """Synchronous convenience wrapper yielding full response text (compatible with TaskDecompositionPlanner)."""
        stream = self.generate_stream(
            system_prompt=system_prompt,
            messages=messages,
            tools=tools,
            cancellation_token=cancellation_token,
            request_config=request_config
        )
        parts = []
        for chunk in stream:
            if chunk.text_delta:
                parts.append(chunk.text_delta)
        return "".join(parts)

    def generate_stream(
        self,
        system_prompt: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        cancellation_token: Optional[Any] = None,
        request_config: Optional[InferenceRequestConfig] = None
    ) -> Iterator[ModelStreamChunk]:
        """Streams normalized response chunks with automatic fault detection and local failover."""
        token = cancellation_token or (request_config.cancellation_token if request_config else None)
        if token and getattr(token, "is_cancelled", False):
            return

        cfg = request_config or InferenceRequestConfig()
        effective_cid = cfg.conversation_id or self._conversation_id or ""

        # 1. Resolve candidate provider list
        with self._lock:
            candidates: List[str] = []
            if cfg.provider and cfg.provider in self._providers:
                candidates = [cfg.provider]
            else:
                # Check turn-local active provider for THIS conversation
                session_active = self._session_active_providers.get(effective_cid)
                if session_active and session_active in self._providers:
                    candidates.append(session_active)
                elif self._primary_provider in self._providers:
                    candidates.append(self._primary_provider)

                for f_name in self._fallback_order:
                    if f_name in self._providers and f_name not in candidates:
                        candidates.append(f_name)

        if not candidates:
            raise RuntimeError("MultiProviderCoordinator has no candidate providers to dispatch inference request.")

        # 2. Evaluate cooldowns and select starting provider
        last_error: Optional[Exception] = None
        last_failure_cat: Optional[FailureCategory] = None
        last_failed_prov: Optional[str] = None
        has_fallen_back = False

        for idx, p_name in enumerate(candidates):
            if token and getattr(token, "is_cancelled", False):
                return

            with self._lock:
                prov = self._providers[p_name]
                circuit = self._circuits.get(p_name, ProviderCircuit(p_name))

            in_cd, rem = circuit.is_in_cooldown()

            # If provider is currently in cooldown and alternative candidates exist, skip it
            if in_cd and len(candidates) > 1 and idx < len(candidates) - 1:
                logger.info(f"Skipping provider '{p_name}' (in cooldown for {rem:.1f}s).")
                continue

            # Fallback eligibility check (if not the initial candidate)
            if idx > 0 and last_failure_cat and last_failed_prov:
                eligible, inelig_reason = is_fallback_eligible(
                    config=cfg,
                    category=last_failure_cat,
                    failed_provider=last_failed_prov,
                    target_provider=p_name
                )
                if not eligible:
                    logger.warning(f"Fallback to '{p_name}' rejected: {inelig_reason}")
                    self._emit_event("PROVIDER_FALLBACK_FAILED", {
                        "failed_provider": last_failed_prov,
                        "target_provider": p_name,
                        "reason": inelig_reason,
                        "category": last_failure_cat.value
                    }, conversation_id=effective_cid)
                    # Preserves task state: raise honest, truthful error (Part 4)
                    raise RuntimeError(f"Task ineligible for fallback to '{p_name}': {inelig_reason}")

            # Notify provider selected / fallback started
            if idx == 0 and not has_fallen_back:
                self._emit_event("PROVIDER_SELECTED", {
                    "provider": p_name,
                    "model": prov.model_id,
                    "purpose": cfg.purpose,
                    "is_fallback": False
                }, conversation_id=effective_cid)
            else:
                has_fallen_back = True
                self._emit_event("PROVIDER_FALLBACK_STARTED", {
                    "from_provider": last_failed_prov,
                    "to_provider": p_name,
                    "to_model": prov.model_id,
                    "reason": str(last_error)[:120] if last_error else "Failover",
                    "category": last_failure_cat.value if last_failure_cat else "UNKNOWN"
                }, conversation_id=effective_cid)

            # Attempt stream execution
            chunks_yielded = 0
            has_yielded_tool_call = False
            start_time = time.time()

            try:
                # Prepare provider-specific request-scoped options
                kwargs: Dict[str, Any] = {
                    "system_prompt": system_prompt,
                    "messages": messages,
                    "tools": tools,
                    "cancellation_token": token,
                }
                # Forward request_config to underlying provider if supported
                import inspect
                try:
                    sig = inspect.signature(prov.generate_stream)
                    if "request_config" in sig.parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
                        kwargs["request_config"] = cfg
                except Exception:
                    pass

                # Support request-scoped Mode A / Mode B think parameter on Ollama without mutating instance
                if p_name == "ollama" and hasattr(prov, "_think"):
                    if cfg.mode == "routine":
                        kwargs["think"] = False
                    elif cfg.mode == "reasoning":
                        kwargs["think"] = True

                stream_iter = prov.generate_stream(**kwargs)

                for chunk in stream_iter:
                    if token and getattr(token, "is_cancelled", False):
                        return

                    # Track yielded output for streaming safety (Part 5)
                    if chunk.has_content() or chunk.thinking_status:
                        chunks_yielded += 1
                    if chunk.tool_calls:
                        has_yielded_tool_call = True

                    yield chunk

                # Stream completed successfully!
                circuit.record_success()
                with self._lock:
                    if effective_cid:
                        self._session_active_providers[effective_cid] = p_name

                if has_fallen_back:
                    self._emit_event("PROVIDER_FALLBACK_COMPLETED", {
                        "active_provider": p_name,
                        "active_model": prov.model_id,
                        "duration_seconds": time.time() - start_time
                    }, conversation_id=effective_cid)

                return  # Turn successfully completed

            except Exception as stream_err:
                last_error = stream_err
                last_failed_prov = p_name
                cat, reason, retry_sec = classify_provider_error(stream_err)
                last_failure_cat = cat

                # Cooldown calculation distinguishing burst rate limits, quota exhaustion, and outages
                if cat in (FailureCategory.CONTEXT_LIMIT, FailureCategory.INVALID_REQUEST):
                    cd_duration = 0.0
                elif retry_sec:
                    cd_duration = retry_sec
                elif cat == FailureCategory.QUOTA_EXHAUSTED:
                    # Daily quota exhaustion defaults to 86400s (24h) unless midnight calculation yielded a value
                    now_utc = datetime.now(timezone.utc)
                    midnight_utc = (now_utc + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                    cd_duration = max(3600.0, (midnight_utc - now_utc).total_seconds())
                elif cat == FailureCategory.RATE_LIMIT:
                    cd_duration = self._rate_limit_cooldown
                else:
                    cd_duration = self._upstream_cooldown

                circuit.trip(cd_duration, reason, cat)

                self._emit_event("PROVIDER_FAILURE", {
                    "provider": p_name,
                    "model": prov.model_id,
                    "category": cat.value,
                    "error": str(stream_err)[:200],
                    "cooldown_applied": cd_duration
                }, conversation_id=effective_cid)
                self._emit_event("PROVIDER_COOLDOWN_UPDATED", {
                    "provider": p_name,
                    "cooldown_seconds": cd_duration,
                    "category": cat.value
                }, conversation_id=effective_cid)

                logger.warning(
                    f"Provider '{p_name}' failed with {cat.value}: {stream_err}. "
                    f"Yielded chunks before failure: {chunks_yielded}."
                )

                # Streaming UX safety (Part 5):
                # If partial text was already emitted before failing mid-stream,
                # emit a clean reset signal so AgentExecutor and ChatView do NOT merge
                # incomplete failed-provider text with fallback text.
                if chunks_yielded > 0:
                    yield ModelStreamChunk(
                        thinking_status=f"\n[Provider '{p_name}' failed mid-stream ({cat.value}). Resetting buffer and failing over to local {prov.model_id}...]\n",
                        finish_reason="PROVIDER_RESET"
                    )

                # Continue loop to next candidate
                continue

        # If all candidates exhausted
        self._emit_event("PROVIDER_FALLBACK_FAILED", {
            "all_candidates": candidates,
            "last_error": str(last_error) if last_error else "Exhausted"
        }, conversation_id=effective_cid)
        raise RuntimeError(
            f"All candidate providers ({', '.join(candidates)}) failed. Last error: {last_error}"
        ) from last_error

    def close(self) -> None:
        """Closes all underlying provider instances and releases sockets."""
        with self._lock:
            providers_copy = dict(self._providers)
        for name, p in providers_copy.items():
            try:
                p.close()
            except Exception as e:
                logger.debug(f"Error closing provider '{name}': {e}")
