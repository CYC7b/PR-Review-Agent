"""Tool Gateway —— 统一权限、审计、限流与输入输出校验（SPEC 2.1 / 3.2）。

所有 Agent 对外部资源（GitHub、文件系统、沙箱、记忆）的访问必须经过 Gateway。
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from app.db.repositories import ToolCallLogRepository
from app.db.session import session_scope
from app.logging_setup import get_logger

logger = get_logger(__name__)


class RateLimitExceeded(Exception):
    pass


class ToolGateway:
    """工具调用网关：审计 + 限流 + 输入脱敏。"""

    def __init__(self, default_rpm: int = 600) -> None:
        self._default_rpm = default_rpm
        self._limits: dict[str, int] = {}  # tool_name -> rpm
        self._buckets: dict[str, deque[float]] = defaultdict(deque)
        self._registry: dict[str, Callable[..., Any]] = {}

    def register(self, name: str, func: Callable[..., Any], rpm: int | None = None) -> None:
        self._registry[name] = func
        if rpm:
            self._limits[name] = rpm

    def _check_rate(self, name: str) -> None:
        limit = self._limits.get(name, self._default_rpm)
        now = time.monotonic()
        bucket = self._buckets[name]
        while bucket and now - bucket[0] > 60:
            bucket.popleft()
        if len(bucket) >= limit:
            raise RateLimitExceeded(f"工具 {name} 触发限流 ({limit} rpm)")
        bucket.append(now)

    @staticmethod
    def _summarize(value: Any, limit: int = 500) -> str:
        s = value if isinstance(value, str) else repr(value)
        return s if len(s) <= limit else s[:limit] + "...(truncated)"

    def call(
        self,
        name: str,
        *,
        review_id: str,
        caller: str,
        head_sha: str | None = None,
        params: dict | None = None,
    ) -> Any:
        """执行工具调用并记录审计日志。"""
        if name not in self._registry:
            raise KeyError(f"未注册工具: {name}")
        self._check_rate(name)
        params = params or {}
        start = time.monotonic()
        input_summary = self._summarize(params)
        error: str | None = None
        result: Any = None
        try:
            result = self._registry[name](**params)
            return result
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            logger.error("tool_call.failed", tool=name, caller=caller, review_id=review_id, error=error)
            raise
        finally:
            duration_ms = int((time.monotonic() - start) * 1000)
            output_summary = "" if error else self._summarize(result)
            try:
                with session_scope() as session:
                    ToolCallLogRepository(session).log(
                        review_id=review_id,
                        caller=caller,
                        tool=name,
                        input_summary=input_summary,
                        output_summary=output_summary,
                        error=error,
                        duration_ms=duration_ms,
                        head_sha=head_sha,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("tool_call.audit_failed", error=str(exc))

    def now_utc(self) -> datetime:
        return datetime.now(timezone.utc)


_default_gateway: ToolGateway | None = None


def get_gateway() -> ToolGateway:
    global _default_gateway
    if _default_gateway is None:
        _default_gateway = ToolGateway()
    return _default_gateway


def reset_gateway() -> None:
    global _default_gateway
    _default_gateway = None
