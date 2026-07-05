"""结构化日志（structlog），遵循 SPEC 17.2 日志安全要求。"""

from __future__ import annotations

import logging
import re
from typing import Any

import structlog

_SENSITIVE_KEYS = {
    "token", "secret", "password", "passwd", "api_key", "apikey",
    "private_key", "authorization", "cookie", "credential",
    "github_token", "llm_api_key", "webhook_secret",
}
_SECRET_PATTERN = re.compile(r"(?i)(token|secret|password|key|authorization)\s*[=:]\s*\S+")
_REDACTED = "***REDACTED***"


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _SECRET_PATTERN.sub(lambda m: m.group(0).split("=")[0] + "=***REDACTED***", value)
    return value


def _redact_keys(_, __, event_dict: dict) -> dict:
    for key in list(event_dict.keys()):
        if key.lower() in _SENSITIVE_KEYS:
            event_dict[key] = _REDACTED
        elif isinstance(event_dict[key], dict):
            for inner in list(event_dict[key].keys()):
                if inner.lower() in _SENSITIVE_KEYS:
                    event_dict[key][inner] = _REDACTED
    return event_dict


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, level.upper(), logging.INFO),
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            _redact_keys,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
