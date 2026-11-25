"""Presence Runtime Configuration & Operational Mode Policy
Defines runtime execution policies, including the authoritative Local-Only Mode policy.
When Local-Only Mode is active:
- All external/cloud AI providers (Gemini, FreeLLMAPI, OpenRouter, Cloudflare, etc.) are strictly disabled.
- Only local inference (Ollama with Qwen 3.5 9B / Qwen 3 VL 2B, or Fake in unit tests) is permitted.
- Cloud credentials cannot be loaded, discovered, or utilized.
- Network access for normal user applications (Outlook, browsers) remains unaffected.
"""

import os
from typing import Set

# Canonical set of cloud AI providers that are strictly forbidden in local-only mode
CLOUD_PROVIDERS: Set[str] = {
    "gemini",
    "freellmapi",
    "openrouter",
    "cloudflare",
    "openai",
    "anthropic",
}

# Permitted local providers
LOCAL_PROVIDERS: Set[str] = {
    "ollama",
    "fake",
}

# Runtime override flag (None means read from environment)
_runtime_local_only_override: bool = None


def is_local_only_mode() -> bool:
    """Returns True if Presence is operating in strict Local-Only Mode.
    Prioritizes runtime override flag if set, then environment variable PRESENCE_LOCAL_ONLY.
    Defaults to True to enforce the Local-First Computer Companion directive.
    """
    global _runtime_local_only_override
    if _runtime_local_only_override is not None:
        return _runtime_local_only_override

    val = os.environ.get("PRESENCE_LOCAL_ONLY", "1").strip().lower()
    return val in ("1", "true", "yes", "on", "enabled")


def set_local_only_mode(enabled: bool) -> None:
    """Explicitly sets or overrides local-only mode at runtime."""
    global _runtime_local_only_override
    _runtime_local_only_override = enabled
    if enabled:
        os.environ["PRESENCE_LOCAL_ONLY"] = "1"
    else:
        os.environ["PRESENCE_LOCAL_ONLY"] = "0"


def is_cloud_provider(name: str) -> bool:
    """Returns True if the specified provider identifier corresponds to an external cloud AI service."""
    if not name:
        return False
    return name.strip().lower() in CLOUD_PROVIDERS


def get_default_local_model() -> str:
    """Returns the configured primary local text reasoning model ID."""
    return os.environ.get("OLLAMA_MODEL", "qwen3.5:9b").strip()


def get_default_vision_model() -> str:
    """Returns the configured primary local vision perception model ID."""
    return os.environ.get("OLLAMA_VISION_MODEL", "qwen3-vl:2b").strip()
