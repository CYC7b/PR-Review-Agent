"""Memory Tool —— 长期记忆查询/存储/删除（SPEC 8.4 / 第 11 节）。

默认仅在 repo scope 启用；跨 repo/org 必须显式开启。
写入前脱敏，禁止存储完整私有代码（SPEC 11.2）。
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from app.config import get_config
from app.db.repositories import MemoryRepository
from app.db.session import session_scope
from app.logging_setup import get_logger
from app.models import MemoryScope

logger = get_logger(__name__)

# 禁止存储的敏感模式
_SENSITIVE_PATTERNS = [
    re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd)\b\s*[=:]\s*\S{6,}"),
    re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----"),
    re.compile(r"(?i)(AKIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ASIA)[A-Z0-9]{16}"),  # AWS
]

# 完整代码块检测：连续多行代码（>15行）视为完整代码片段
_CODE_BLOCK_RE = re.compile(r"(\n[ \t]*[^\s].*){15,}", re.MULTILINE)


class MemoryTool:
    def _scope_allowed(self, scope: MemoryScope) -> bool:
        cfg = get_config().memory
        if scope == MemoryScope.TASK:
            return True
        if scope == MemoryScope.REPO:
            return cfg.enable_repo_memory
        if scope == MemoryScope.ORG:
            return cfg.enable_org_memory
        if scope == MemoryScope.GLOBAL:
            return True  # 仅允许公共脱敏抽象规则
        return False

    def _sanitize(self, content: str) -> str:
        """脱敏：移除 secrets、移除完整代码块、抽象化（SPEC 11.3）。"""
        sanitized = content

        def _redact(match: re.Match) -> str:
            # 若匹配含捕获组（键名），保留键名仅替换值
            if match.groups():
                return f"{match.group(1)}=***REDACTED***"
            return "***REDACTED***"

        for pattern in _SENSITIVE_PATTERNS:
            sanitized = pattern.sub(_redact, sanitized)
        # 拒绝存储完整代码块
        if _CODE_BLOCK_RE.search(sanitized) and not get_config().memory.store_full_code:
            logger.warning("memory.refused_full_code_block")
            # 截断为摘要
            sanitized = sanitized[:200] + "...(完整代码已移除，仅保留摘要)"
        return sanitized

    def query(self, scope: str, query: str, repo_id: int | None = None) -> list[dict[str, Any]]:
        """memory.query —— 查询抽象模式。"""
        sc = MemoryScope(scope)
        if not self._scope_allowed(sc):
            logger.info("memory.scope_disabled", scope=scope)
            return []
        with session_scope() as session:
            return MemoryRepository(session).query(sc, query, repo_id=repo_id)

    def store(
        self,
        scope: str,
        insight_object: dict[str, Any],
        repo_id: int | None = None,
    ) -> dict[str, Any]:
        """memory.store —— 存储脱敏后的抽象规则。"""
        sc = MemoryScope(scope)
        if not self._scope_allowed(sc):
            logger.info("memory.store.scope_disabled", scope=scope)
            return {"stored": False, "reason": "scope_disabled"}
        content = self._sanitize(str(insight_object.get("content", "")))
        object_id = insight_object.get("object_id") or f"mem-{uuid.uuid4().hex[:12]}"
        insight_type = insight_object.get("type", "")
        with session_scope() as session:
            MemoryRepository(session).store(
                scope=sc,
                object_id=object_id,
                content=content,
                insight_type=insight_type,
                repo_id=repo_id,
                metadata=insight_object.get("metadata", {}),
            )
        return {"stored": True, "object_id": object_id}

    def delete(self, scope: str, object_id: str) -> dict[str, Any]:
        """memory.delete —— 删除记忆条目。"""
        sc = MemoryScope(scope)
        with session_scope() as session:
            ok = MemoryRepository(session).delete(sc, object_id)
        return {"deleted": ok, "object_id": object_id}


_default_memory: MemoryTool | None = None


def get_memory_tool() -> MemoryTool:
    global _default_memory
    if _default_memory is None:
        _default_memory = MemoryTool()
    return _default_memory
