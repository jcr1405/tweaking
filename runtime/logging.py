"""Diagnostics and Structured Logging Subsystem
Configures structured file and console logging with automatic
PII and credential redaction (Bearer tokens, API keys, passwords).
"""

import os
import re
import sys
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

# Regex patterns for sensitive tokens
REDACTION_PATTERNS = [
    (re.compile(r"(Bearer\s+)[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE), r"\1[REDACTED_TOKEN]"),
    (re.compile(r"(sk-[A-Za-z0-9]{20,})", re.IGNORECASE), "[REDACTED_API_KEY]"),
    (re.compile(r"(AIza[0-9A-Za-z-_]{35})", re.IGNORECASE), "[REDACTED_GOOGLE_KEY]"),
    (re.compile(r"(password\s*[:=]\s*)['\"][^'\"]+['\"]", re.IGNORECASE), r"\1'[REDACTED_PASSWORD]'"),
    (re.compile(r"(token\s*[:=]\s*)['\"][^'\"]+['\"]", re.IGNORECASE), r"\1'[REDACTED_TOKEN]'"),
]


class SanitizingFilter(logging.Filter):
    """Logging filter that scrubs sensitive credentials from record messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            msg = record.msg
            for pattern, replacement in REDACTION_PATTERNS:
                msg = pattern.sub(replacement, msg)
            record.msg = msg

        if record.args:
            if isinstance(record.args, dict):
                clean_args = {}
                for k, v in record.args.items():
                    if isinstance(v, str):
                        for pattern, replacement in REDACTION_PATTERNS:
                            v = pattern.sub(replacement, v)
                    clean_args[k] = v
                record.args = clean_args
            elif isinstance(record.args, tuple):
                clean_args = []
                for v in record.args:
                    if isinstance(v, str):
                        for pattern, replacement in REDACTION_PATTERNS:
                            v = pattern.sub(replacement, v)
                    clean_args.append(v)
                record.args = tuple(clean_args)

        return True


def get_default_log_dir() -> Path:
    """Returns the default directory for runtime logs."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        path = Path(local_app_data) / "Presence" / "logs"
    else:
        path = Path("./logs").resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def setup_logger(
    name: str = "presence",
    log_dir: Optional[Path] = None,
    log_level: int = logging.INFO,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
    console_output: bool = True,
) -> logging.Logger:
    """Configures and returns the central structured logger."""
    logger = logging.getLogger(name)
    logger.setLevel(log_level)

    # Avoid duplicate handlers if called multiple times
    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        fmt="[%(asctime)s.%(msecs)03d] [%(levelname)s] [%(name)s.%(filename)s:%(lineno)d] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    sanitizer = SanitizingFilter()

    # 1. Console Handler
    if console_output:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(log_level)
        console_handler.setFormatter(formatter)
        console_handler.addFilter(sanitizer)
        logger.addHandler(console_handler)

    # 2. Rotating File Handler
    try:
        target_dir = log_dir or get_default_log_dir()
        file_path = target_dir / "runtime.log"
        file_handler = RotatingFileHandler(
            file_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(log_level)
        file_handler.setFormatter(formatter)
        file_handler.addFilter(sanitizer)
        logger.addHandler(file_handler)
    except Exception as e:
        logger.warning(f"Failed to initialize file logger at {target_dir}: {e}")

    return logger


def close_logger(name: str = "presence") -> None:
    """Closes and removes all handlers from the specified logger, releasing open file locks."""
    logger = logging.getLogger(name)
    for handler in list(logger.handlers):
        try:
            handler.close()
            logger.removeHandler(handler)
        except Exception:
            pass
