"""Permission and Action Boundary Subsystem
Enforces the non-bypassable 4-Tier Authority Gate on all execution handles.
Models and user commands are untrusted; all actions must be formally authorized.
"""

from enum import IntEnum, unique
import json
import logging
import time
import secrets
import hashlib
import threading
from typing import Dict, Optional, Any, Callable
from dataclasses import dataclass

logger = logging.getLogger("presence.permissions")


@unique
class AuthorityLevel(IntEnum):
    """The Four Authority Levels governing system operations."""
    L0_OBSERVE = 0            # Read-only observation, telemetry, status
    L1_RECOMMEND = 1          # Synthesizing suggestions, preparing diffs
    L2_EXECUTE_REVERSIBLE = 2 # Reversible local writes, config updates
    L3_PROTECTED = 3          # Irreversible actions, system kill, disk format, credentials


# Alias for compatibility with architectural specifications
PermissionTier = AuthorityLevel


class PermissionDeniedError(Exception):
    """Raised when an operation fails authorization policy."""
    pass


@dataclass(frozen=True)
class ActionPolicy:
    action_name: str
    required_level: AuthorityLevel
    description: str
    requires_explicit_consent: bool = False


@dataclass(frozen=True)
class AuthorizationResult:
    allowed: bool
    action_name: str
    level: AuthorityLevel
    reason: str
    user_consent_required: bool = False


def _validate_and_normalize_canonical_data(obj: Any) -> Any:
    """Recursively validates JSON primitives, strictly enforces string dict keys,
    normalizes -0.0 to 0.0 per RFC 8785, and rejects non-JSON / non-finite values."""
    if obj is None or isinstance(obj, (str, bool, int)):
        return obj
    elif isinstance(obj, float):
        import math
        if math.isnan(obj) or math.isinf(obj):
            raise ValueError(f"Non-finite float value {obj} is rejected in canonical arguments")
        # Normalize -0.0 to 0.0 per RFC 8785 section 3.2.2.3
        if obj == 0.0:
            return 0.0
        return obj
    elif isinstance(obj, dict):
        normalized_dict = {}
        for k, v in obj.items():
            if not isinstance(k, str):
                raise TypeError(f"All dictionary keys must be strings, got {type(k).__name__}: {k!r}")
            normalized_dict[k] = _validate_and_normalize_canonical_data(v)
        return normalized_dict
    elif isinstance(obj, (list, tuple)):
        return [_validate_and_normalize_canonical_data(item) for item in obj]
    else:
        raise TypeError(f"Unsupported non-JSON data type in tool arguments: {type(obj).__name__}")


def canonicalize_arguments(args: Optional[Dict[str, Any]]) -> str:
    """Produces a deterministic, canonical JSON representation of tool arguments.
    Sorts all dictionary keys recursively and enforces strict comma/colon separators.
    Validates that all dictionary keys are strings and normalizes -0.0 to 0.0 per RFC 8785.
    Rejects non-finite floating point numbers (NaN, Infinity) and non-JSON objects.
    """
    if args is None:
        return "{}"
    if not isinstance(args, dict):
        raise ValueError(f"Tool arguments must be a dictionary, got {type(args).__name__}")

    normalized = _validate_and_normalize_canonical_data(args)
    return json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False
    )


def compute_argument_hash(args: Optional[Dict[str, Any]]) -> str:
    """Computes a SHA-256 hex digest of the canonicalized argument representation."""
    canonical_json = canonicalize_arguments(args)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IssuedConsent:
    token_hash: str
    action_name: str
    argument_hash: str
    conversation_id: str
    tier: AuthorityLevel
    expires_at: float


ConsentProvider = Callable[[str, Dict[str, Any]], bool]


class PermissionBoundary:
    """Central authority gatekeeper. Mediates all subsystem and tool execution requests."""

    def __init__(self, consent_provider: Optional[ConsentProvider] = None):
        self._policies: Dict[str, ActionPolicy] = {}
        self._consent_provider = consent_provider
        self._active_consent_tokens: Dict[str, IssuedConsent] = {}
        self._lock = threading.Lock()
        self._register_default_policies()

    def _cleanup_expired_tokens_locked(self, now: Optional[float] = None) -> None:
        """Purges expired consent tokens from the internal registry."""
        curr_time = now or time.monotonic()
        expired_keys = [k for k, v in self._active_consent_tokens.items() if curr_time > v.expires_at]
        for k in expired_keys:
            del self._active_consent_tokens[k]

    def issue_consent_token(
        self,
        action_name: str,
        context: Optional[Dict[str, Any]] = None,
        conversation_id: str = "",
        ttl_seconds: float = 60.0
    ) -> str:
        """Issues a single-use cryptographically random consent token bound to exact arguments."""
        raw_token = f"cst_{secrets.token_urlsafe(32)}"
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        arg_hash = compute_argument_hash(context)

        policy = self._policies.get(action_name)
        tier = policy.required_level if policy else AuthorityLevel.L3_PROTECTED

        now = time.monotonic()
        with self._lock:
            self._cleanup_expired_tokens_locked(now)
            self._active_consent_tokens[token_hash] = IssuedConsent(
                token_hash=token_hash,
                action_name=action_name,
                argument_hash=arg_hash,
                conversation_id=conversation_id,
                tier=tier,
                expires_at=now + ttl_seconds
            )
        logger.info(
            f"Issued single-use consent token for '{action_name}' "
            f"[arg_digest={arg_hash[:8]}...] (TTL: {ttl_seconds}s)"
        )
        return raw_token

    def verify_consent_token(
        self,
        token: str,
        action_name: str,
        context: Optional[Dict[str, Any]] = None,
        conversation_id: str = "",
        expected_tier: Optional[AuthorityLevel] = None
    ) -> bool:
        """Atomically verifies and consumes a single-use consent token with strict argument and tier validation."""
        import hmac

        if not token or not isinstance(token, str):
            return False

        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = time.monotonic()

        with self._lock:
            consent = self._active_consent_tokens.pop(token_hash, None)

        if not consent:
            logger.warning(f"Invalid or already-consumed consent token presented for action '{action_name}'.")
            return False

        if now > consent.expires_at:
            logger.warning(
                f"Expired consent token presented for action '{action_name}' "
                f"(expired {now - consent.expires_at:.1f}s ago)."
            )
            return False

        if consent.action_name != action_name:
            logger.warning(
                f"Consent token action mismatch: expected '{consent.action_name}', got '{action_name}'."
            )
            return False

        # Validate against registered policy and expected permission tier
        current_policy = self._policies.get(action_name)
        if not current_policy:
            logger.warning(f"No policy registered for action '{action_name}'. Failing closed.")
            return False

        if current_policy.required_level != consent.tier:
            logger.warning(
                f"Consent token tier mismatch for '{action_name}': "
                f"token tier is {consent.tier.name}, but current policy required level is {current_policy.required_level.name}."
            )
            return False

        if expected_tier is not None and (consent.tier != expected_tier or current_policy.required_level != expected_tier):
            logger.warning(
                f"Consent token expected tier mismatch for '{action_name}': "
                f"expected {expected_tier.name}, got consent tier {consent.tier.name}."
            )
            return False

        # Strict canonical argument validation with constant-time comparison
        try:
            presented_arg_hash = compute_argument_hash(context)
        except Exception as e:
            logger.warning(f"Failed to canonicalize presented arguments for '{action_name}': {e}")
            return False

        if not hmac.compare_digest(consent.argument_hash, presented_arg_hash):
            logger.warning(
                f"Consent token argument mismatch for '{action_name}': "
                f"approved args digest ({consent.argument_hash[:8]}...) did not match "
                f"presented args digest ({presented_arg_hash[:8]}...)."
            )
            return False

        if consent.conversation_id and conversation_id and consent.conversation_id != conversation_id:
            logger.warning(
                f"Consent token conversation mismatch: expected '{consent.conversation_id}', got '{conversation_id}'."
            )
            return False

        logger.info(f"Verified and consumed valid consent token for '{action_name}' [tier={consent.tier.name}].")
        return True

    def _register_default_policies(self) -> None:
        """Registers foundational action policies for the runtime."""
        # L0: Observe
        self.register_policy(ActionPolicy("system.status", AuthorityLevel.L0_OBSERVE, "Query runtime and health metrics"))
        self.register_policy(ActionPolicy("config.read", AuthorityLevel.L0_OBSERVE, "Read runtime configuration"))
        self.register_policy(ActionPolicy("help.read", AuthorityLevel.L0_OBSERVE, "Read available commands"))

        # L2: Execute Reversible
        self.register_policy(ActionPolicy("config.write", AuthorityLevel.L2_EXECUTE_REVERSIBLE, "Update persistent configuration"))
        self.register_policy(ActionPolicy("runtime.shutdown", AuthorityLevel.L2_EXECUTE_REVERSIBLE, "Order graceful shutdown"))

        # L3: Protected
        self.register_policy(ActionPolicy("system.process_kill", AuthorityLevel.L3_PROTECTED, "Terminate external system process", requires_explicit_consent=True))
        self.register_policy(ActionPolicy("fs.delete_file", AuthorityLevel.L3_PROTECTED, "Delete filesystem path", requires_explicit_consent=True))
        self.register_policy(ActionPolicy("credentials.write", AuthorityLevel.L3_PROTECTED, "Mutate credential vault", requires_explicit_consent=True))

    def register_policy(self, policy: ActionPolicy) -> None:
        """Registers or overrides an action policy."""
        self._policies[policy.action_name] = policy

    def check_permission(
        self,
        action_name: str,
        context: Optional[Dict[str, Any]] = None,
        granted_consent: bool = False,
    ) -> AuthorizationResult:
        """Evaluates whether an action is permitted under the active authority matrix."""
        policy = self._policies.get(action_name)
        if not policy:
            logger.warning(f"Unregistered action requested: {action_name}. Rejecting by default (fail-closed).")
            return AuthorizationResult(
                allowed=False,
                action_name=action_name,
                level=AuthorityLevel.L3_PROTECTED,
                reason=f"Action '{action_name}' is not registered in the authority matrix (fail-closed policy).",
            )

        # L0 and L1 are autonomous
        if policy.required_level in (AuthorityLevel.L0_OBSERVE, AuthorityLevel.L1_RECOMMEND):
            return AuthorizationResult(
                allowed=True,
                action_name=action_name,
                level=policy.required_level,
                reason="Autonomous execution permitted under L0/L1 policy.",
            )

        # L2 is autonomous if policy does not mandate explicit consent
        if policy.required_level == AuthorityLevel.L2_EXECUTE_REVERSIBLE:
            if not policy.requires_explicit_consent:
                return AuthorizationResult(
                    allowed=True,
                    action_name=action_name,
                    level=policy.required_level,
                    reason="Autonomous execution permitted under L2 reversible policy.",
                )

        # L3 or explicit consent required
        if policy.requires_explicit_consent or policy.required_level >= AuthorityLevel.L3_PROTECTED:
            if granted_consent:
                return AuthorizationResult(
                    allowed=True,
                    action_name=action_name,
                    level=policy.required_level,
                    reason="Explicit consent granted.",
                    user_consent_required=True,
                )

            # Check dynamic consent provider if configured
            if self._consent_provider and self._consent_provider(action_name, context or {}):
                return AuthorizationResult(
                    allowed=True,
                    action_name=action_name,
                    level=policy.required_level,
                    reason="Consent confirmed via interactive consent provider.",
                    user_consent_required=True,
                )

            return AuthorizationResult(
                allowed=False,
                action_name=action_name,
                level=policy.required_level,
                reason=f"Action '{action_name}' requires explicit user consent (Level {policy.required_level.name}).",
                user_consent_required=True,
            )

        return AuthorizationResult(
            allowed=False,
            action_name=action_name,
            level=policy.required_level,
            reason="Action denied by policy default.",
        )

    def authorize(
        self,
        action_name: str,
        context: Optional[Dict[str, Any]] = None,
        granted_consent: bool = False,
    ) -> None:
        """Enforces permission check; raises PermissionDeniedError if unauthorized."""
        result = self.check_permission(action_name, context, granted_consent)
        if not result.allowed:
            logger.security_violation = True
            logger.warning(f"SECURITY GATE BLOCKED: {action_name} - {result.reason}")
            raise PermissionDeniedError(result.reason)
        logger.debug(f"Action authorized: {action_name} ({result.reason})")
