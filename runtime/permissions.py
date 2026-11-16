"""Permission and Action Boundary Subsystem
Enforces the non-bypassable 4-Tier Authority Gate on all execution handles.
Models and user commands are untrusted; all actions must be formally authorized.
"""

from enum import IntEnum, unique
import logging
import time
import secrets
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


@dataclass(frozen=True)
class IssuedConsent:
    token: str
    action_name: str
    expires_at: float
    context_keys: tuple


ConsentProvider = Callable[[str, Dict[str, Any]], bool]


class PermissionBoundary:
    """Central authority gatekeeper. Mediates all subsystem and tool execution requests."""

    def __init__(self, consent_provider: Optional[ConsentProvider] = None):
        self._policies: Dict[str, ActionPolicy] = {}
        self._consent_provider = consent_provider
        self._active_consent_tokens: Dict[str, IssuedConsent] = {}
        self._register_default_policies()

    def issue_consent_token(
        self,
        action_name: str,
        context: Optional[Dict[str, Any]] = None,
        ttl_seconds: float = 60.0
    ) -> str:
        """Issues a single-use cryptographically random consent token for an action."""
        token = f"cst_{secrets.token_urlsafe(32)}"
        context_keys = tuple(sorted(context.keys())) if context else ()
        self._active_consent_tokens[token] = IssuedConsent(
            token=token,
            action_name=action_name,
            expires_at=time.time() + ttl_seconds,
            context_keys=context_keys
        )
        logger.info(f"Issued single-use consent token for '{action_name}' (TTL: {ttl_seconds}s)")
        return token

    def verify_consent_token(
        self,
        token: str,
        action_name: str,
        context: Optional[Dict[str, Any]] = None
    ) -> bool:
        """Verifies and consumes a single-use consent token. Returns True if valid, False otherwise."""
        if not token or not isinstance(token, str):
            return False

        consent = self._active_consent_tokens.pop(token, None)
        if not consent:
            logger.warning(f"Invalid or already-consumed consent token presented for action '{action_name}'.")
            return False

        now = time.time()
        if now > consent.expires_at:
            logger.warning(f"Expired consent token presented for action '{action_name}' (expired {now - consent.expires_at:.1f}s ago).")
            return False

        if consent.action_name != action_name:
            logger.warning(f"Consent token action mismatch: expected '{consent.action_name}', got '{action_name}'.")
            return False

        logger.info(f"Verified and consumed valid consent token for '{action_name}'.")
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
