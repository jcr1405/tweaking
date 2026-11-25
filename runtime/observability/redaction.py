"""Security Redaction & Terminal Output Sanitization
Prevents terminal injection (ANSI escape hijacking) and redacts sensitive credentials
(API keys, tokens, passwords) before displaying in the Terminal Control Center.
"""

import re
from typing import Any, Dict, List, Union

# Regex for stripping ANSI escape sequences (CSI, OSC, etc.)
ANSI_ESCAPE_PATTERN = re.compile(
    r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\][^\x07\x1B]*[\x07\x1B\\])"
)

# Common credential and sensitive pattern matchers
REDACTION_PATTERNS = [
    # Google AI / Gemini API keys (AIza...)
    (re.compile(r"AIza[0-9A-Za-z-_]{30,}"), "[REDACTED_GEMINI_KEY]"),
    # GitHub Personal Access Tokens (ghp_, gho_, ghu_, ghs_, ghr_)
    (re.compile(r"gh[pousr]_[0-9A-Za-z]{30,}"), "[REDACTED_GITHUB_TOKEN]"),
    # OpenAI / Anthropic / generic API keys (sk-...)
    (re.compile(r"sk-[0-9A-Za-z-_]{20,}"), "[REDACTED_API_KEY]"),
    # Bearer tokens
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9\-._~+/]+=*"), "Bearer [REDACTED_TOKEN]"),
    # Authorization header patterns
    (re.compile(r"(?i)(authorization|auth|token|password|secret|api_key)\s*[:=]\s*['\"]?([^'\"\s,;]+)['\"]?"), r"\1=[REDACTED]"),
    # Private / Session tokens
    (re.compile(r"(?i)(session_token|consent_token)\s*=\s*['\"]?[A-Za-z0-9\-_]+['\"]?"), r"\1=[REDACTED_CONSENT]"),
    # File passwords or connection strings
    (re.compile(r"(?i)(mongodb|postgres|mysql|redis)://[^:]+:([^@]+)@"), r"\1://[USER]:[REDACTED_PASS]@"),
]


def sanitize_terminal_output(text: str) -> str:
    """Strips dangerous raw ANSI escape sequences and non-printable control characters
    from untrusted content to prevent terminal emulation escape injection.
    Preserves normal newlines and tabs.
    """
    if not text:
        return ""
    # 1. Remove ANSI escape sequences
    cleaned = ANSI_ESCAPE_PATTERN.sub("", text)
    # 2. Filter out non-printable ASCII controls except \n, \r, \t
    sanitized = "".join(
        ch for ch in cleaned
        if (ord(ch) >= 32 or ch in ("\n", "\r", "\t"))
    )
    return sanitized


def redact_sensitive_string(text: str) -> str:
    """Redacts API keys, credentials, and tokens from a text string."""
    if not text:
        return ""
    redacted = text
    for pattern, replacement in REDACTION_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def sanitize_and_redact(text: str) -> str:
    """Composes terminal sanitization and sensitive credential redaction."""
    return redact_sensitive_string(sanitize_terminal_output(text))


def redact_data_structure(data: Any, max_depth: int = 5) -> Any:
    """Recursively redacts sensitive keys and strings within nested dicts or lists."""
    if max_depth <= 0:
        return "[MAX_DEPTH]"

    if isinstance(data, str):
        return sanitize_and_redact(data)
    elif isinstance(data, dict):
        new_dict = {}
        for k, v in data.items():
            k_str = str(k).lower()
            if any(s in k_str for s in ("password", "secret", "token", "credential", "api_key", "authorization", "cookie")):
                new_dict[k] = "[REDACTED]"
            else:
                new_dict[k] = redact_data_structure(v, max_depth - 1)
        return new_dict
    elif isinstance(data, list):
        return [redact_data_structure(item, max_depth - 1) for item in data]
    elif isinstance(data, tuple):
        return tuple(redact_data_structure(item, max_depth - 1) for item in data)
    return data
