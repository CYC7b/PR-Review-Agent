"""Blackboard Message 模型（SPEC 6.4）。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BlackboardMessage(BaseModel):
    """子 Agent 之间的短期共享消息。"""

    topic: str  # e.g. issues.security.high / plan / context
    review_id: str
    producer: str
    timestamp: datetime = Field(default_factory=utcnow)
    payload: dict[str, Any] = Field(default_factory=dict)
