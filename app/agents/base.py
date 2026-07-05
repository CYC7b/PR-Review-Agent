"""Agent 基类 —— 提供工具调用、审计、取消检查的公共能力（SPEC 7）。"""

from __future__ import annotations

import threading
from typing import Any

from app.logging_setup import get_logger
from app.tools.gateway import ToolGateway, get_gateway

logger = get_logger(__name__)


class AgentCancelled(Exception):
    """任务被取消（PR 更新或用户取消）。"""


class AgentTimeout(Exception):
    """Agent 执行超时。"""


class BaseAgent:
    """所有 Agent 的基类。

    Agent 不直接访问 GitHub、文件系统、网络或执行环境；
    所有动作通过 ToolGateway 完成（SPEC 2.1）。
    """

    name: str = "BaseAgent"

    def __init__(self, review_id: str, head_sha: str, gateway: ToolGateway | None = None) -> None:
        self.review_id = review_id
        self.head_sha = head_sha
        self.gateway = gateway or get_gateway()
        self._cancelled = threading.Event()
        self.logger = get_logger(self.name)

    def cancel(self) -> None:
        self._cancelled.set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def check_cancelled(self) -> None:
        if self.cancelled:
            raise AgentCancelled(f"{self.name} 被取消 (review={self.review_id})")

    def tool(self, name: str, **params: Any) -> Any:
        """通过网关调用工具，自动附带审计上下文。"""
        self.check_cancelled()
        return self.gateway.call(
            name,
            review_id=self.review_id,
            caller=self.name,
            head_sha=self.head_sha,
            params=params,
        )

    def run(self, *args: Any, **kwargs: Any) -> Any:
        """子类实现具体审查逻辑。"""
        raise NotImplementedError
