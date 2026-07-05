"""Shared Blackboard —— 子 Agent 之间的短期共享状态（SPEC 3.2 / 6.4）。

优先使用 Redis；未配置 Redis 时回退到进程内内存实现。
同时将消息落库一份用于审计与故障恢复。
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from app.db.repositories import BlackboardRepository
from app.db.session import session_scope
from app.logging_setup import get_logger
from app.models import BlackboardMessage

logger = get_logger(__name__)


class Blackboard:
    """Blackboard 抽象接口。"""

    def publish(self, message: BlackboardMessage) -> None: ...

    def read(self, review_id: str, topic: str | None = None) -> list[BlackboardMessage]: ...

    def read_topic(self, review_id: str, topic: str) -> list[BlackboardMessage]: ...

    def clear(self, review_id: str) -> None: ...


class InMemoryBlackboard(Blackboard):
    """进程内 Blackboard（开发/测试默认）。"""

    def __init__(self) -> None:
        self._store: dict[str, list[BlackboardMessage]] = defaultdict(list)
        self._persist: bool = True

    def publish(self, message: BlackboardMessage) -> None:
        self._store[message.review_id].append(message)
        if self._persist:
            try:
                with session_scope() as session:
                    BlackboardRepository(session).append(message)
            except Exception as exc:  # noqa: BLE001
                logger.warning("blackboard.persist_failed", error=str(exc))

    def read(self, review_id: str, topic: str | None = None) -> list[BlackboardMessage]:
        msgs = self._store.get(review_id, [])
        if topic:
            return [m for m in msgs if m.topic == topic]
        return list(msgs)

    def read_topic(self, review_id: str, topic: str) -> list[BlackboardMessage]:
        return self.read(review_id, topic)

    def clear(self, review_id: str) -> None:
        self._store.pop(review_id, None)


class RedisBlackboard(Blackboard):
    """基于 Redis 的 Blackboard（生产环境）。

    使用 Redis List 存储每个 review 的消息流，key 形如 bb:{review_id}。
    """

    def __init__(self, redis_client: Any) -> None:
        self._redis = redis_client

    @staticmethod
    def _key(review_id: str) -> str:
        return f"bb:{review_id}"

    def publish(self, message: BlackboardMessage) -> None:
        self._redis.rpush(self._key(message.review_id), message.model_dump_json())
        try:
            with session_scope() as session:
                BlackboardRepository(session).append(message)
        except Exception as exc:  # noqa: BLE001
            logger.warning("blackboard.persist_failed", error=str(exc))

    def read(self, review_id: str, topic: str | None = None) -> list[BlackboardMessage]:
        raw = self._redis.lrange(self._key(review_id), 0, -1) or []
        msgs = []
        for item in raw:
            data = item if isinstance(item, str) else item.decode("utf-8")
            try:
                msg = BlackboardMessage(**json.loads(data))
            except Exception:  # noqa: BLE001
                continue
            if topic is None or msg.topic == topic:
                msgs.append(msg)
        return msgs

    def read_topic(self, review_id: str, topic: str) -> list[BlackboardMessage]:
        return self.read(review_id, topic)

    def clear(self, review_id: str) -> None:
        self._redis.delete(self._key(review_id))


_blackboard: Blackboard | None = None


def get_blackboard() -> Blackboard:
    global _blackboard
    if _blackboard is None:
        from app.config import get_settings

        settings = get_settings()
        if settings.redis_url:
            try:
                import redis  # type: ignore

                client = redis.from_url(settings.redis_url, decode_responses=True)
                client.ping()
                _blackboard = RedisBlackboard(client)
                logger.info("blackboard.mode", mode="redis")
            except Exception as exc:  # noqa: BLE001
                logger.warning("blackboard.redis_unavailable_fallback_inmemory", error=str(exc))
                _blackboard = InMemoryBlackboard()
        else:
            _blackboard = InMemoryBlackboard()
            logger.info("blackboard.mode", mode="inmemory")
    return _blackboard


def reset_blackboard() -> None:
    global _blackboard
    _blackboard = None
