"""Provider Circuit Breaker Subsystem
Protects external search providers from being repeatedly requested after bot challenges,
access restrictions, or rate limit overruns.
"""

import time
import logging
import threading
from typing import Optional, Tuple, Dict, Any, Callable

from runtime.search.models import CircuitState, ResponseClassification

logger = logging.getLogger("presence.search.circuit")


class ProviderCircuitBreaker:
    """Thread-safe circuit breaker with half-open recovery probe semantics."""

    def __init__(
        self,
        provider_name: str,
        cooldown_seconds: float = 1800.0,
        time_fn: Optional[Callable[[], float]] = None,
    ):
        self.provider_name = provider_name
        self.cooldown_seconds = cooldown_seconds
        self._time_fn = time_fn or time.time
        self._lock = threading.Lock()

        self._state = CircuitState.CLOSED
        self._opened_at: float = 0.0
        self._cooldown_until: float = 0.0
        self._failure_count: int = 0
        self._probe_in_flight: bool = False
        self._last_error_reason: str = ""

    @property
    def state(self) -> CircuitState:
        with self._lock:
            self._evaluate_state()
            return self._state

    def _evaluate_state(self) -> None:
        """Internal state transition evaluation based on elapsed time."""
        now = self._time_fn()
        if self._state == CircuitState.OPEN and now >= self._cooldown_until:
            logger.info(
                f"Circuit breaker for provider '{self.provider_name}' cooldown expired ({self.cooldown_seconds}s). "
                f"Transitioning to HALF_OPEN for controlled recovery probe."
            )
            self._state = CircuitState.HALF_OPEN
            self._probe_in_flight = False

    def can_execute(self) -> Tuple[bool, float, CircuitState]:
        """Checks if a request is permitted.

        Returns:
            Tuple of (allowed: bool, cooldown_remaining: float, current_state: CircuitState)
        """
        with self._lock:
            self._evaluate_state()
            now = self._time_fn()

            if self._state == CircuitState.CLOSED:
                return True, 0.0, CircuitState.CLOSED

            if self._state == CircuitState.OPEN:
                remaining = max(0.0, self._cooldown_until - now)
                return False, remaining, CircuitState.OPEN

            if self._state == CircuitState.HALF_OPEN:
                if not self._probe_in_flight:
                    self._probe_in_flight = True
                    logger.info(f"Allowing single recovery probe for provider '{self.provider_name}' in HALF_OPEN state.")
                    return True, 0.0, CircuitState.HALF_OPEN
                else:
                    return False, 0.0, CircuitState.HALF_OPEN

            return False, 0.0, self._state

    def record_success(self) -> None:
        """Records a successful search response. Closes circuit if in HALF_OPEN."""
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                logger.info(f"Recovery probe for '{self.provider_name}' succeeded. Circuit CLOSED.")
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._probe_in_flight = False
            self._last_error_reason = ""

    def record_failure(
        self,
        classification: ResponseClassification,
        reason: str = "",
        custom_cooldown: Optional[float] = None
    ) -> None:
        """Records a provider failure and conditionally transitions to OPEN."""
        with self._lock:
            now = self._time_fn()
            self._failure_count += 1
            self._last_error_reason = reason or classification.value

            # Immediate trip on confirmed bot challenges or access denial
            trip_immediately = classification in (
                ResponseClassification.PROVIDER_CHALLENGED,
                ResponseClassification.ACCESS_DENIED,
                ResponseClassification.RATE_LIMITED,
            )

            # In HALF_OPEN, ANY failure immediately trips circuit back to OPEN
            if self._state == CircuitState.HALF_OPEN or trip_immediately:
                cooldown = custom_cooldown if custom_cooldown is not None else self.cooldown_seconds
                self._state = CircuitState.OPEN
                self._opened_at = now
                self._cooldown_until = now + cooldown
                self._probe_in_flight = False
                logger.warning(
                    f"CIRCUIT OPENED for provider '{self.provider_name}' due to {classification.value} ({reason}). "
                    f"Cooldown set to {cooldown:.1f}s (until {self._cooldown_until:.1f})."
                )

    def reset(self) -> None:
        """Explicit user-controlled reset mechanism."""
        with self._lock:
            logger.info(f"User requested manual reset of circuit breaker for provider '{self.provider_name}'.")
            self._state = CircuitState.CLOSED
            self._opened_at = 0.0
            self._cooldown_until = 0.0
            self._failure_count = 0
            self._probe_in_flight = False
            self._last_error_reason = ""

    def get_status(self) -> Dict[str, Any]:
        """Returns structured diagnostic status dictionary."""
        with self._lock:
            self._evaluate_state()
            now = self._time_fn()
            remaining = max(0.0, self._cooldown_until - now) if self._state == CircuitState.OPEN else 0.0
            return {
                "provider": self.provider_name,
                "circuit_state": self._state.value,
                "cooldown_remaining_seconds": round(remaining, 1),
                "failure_count": self._failure_count,
                "last_error": self._last_error_reason,
                "probe_in_flight": self._probe_in_flight,
            }
