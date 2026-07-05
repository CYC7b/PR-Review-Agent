"""Memory 工具测试（SPEC 8.4 / 第 11 节）。"""

from __future__ import annotations

from app.config import get_config
from app.tools.memory_tool import MemoryTool


class TestMemoryTool:
    def test_store_task_scope_allowed(self):
        tool = MemoryTool()
        result = tool.store("task", {"content": "abstract pattern", "type": "rule"})
        assert result["stored"] is True

    def test_store_repo_scope_disabled_by_default(self):
        tool = MemoryTool()
        result = tool.store("repo", {"content": "test", "type": "rule"}, repo_id=1)
        assert result["stored"] is False
        assert result["reason"] == "scope_disabled"

    def test_store_repo_scope_when_enabled(self):
        cfg = get_config()
        cfg.memory.enable_repo_memory = True
        tool = MemoryTool()
        result = tool.store("repo", {"content": "project convention", "type": "convention"}, repo_id=1)
        assert result["stored"] is True

    def test_query_returns_results(self):
        cfg = get_config()
        cfg.memory.enable_repo_memory = True
        tool = MemoryTool()
        tool.store("repo", {"content": "use parameterized queries", "type": "rule"}, repo_id=1)
        results = tool.query("repo", "parameterized", repo_id=1)
        assert len(results) == 1
        assert "parameterized" in results[0]["content"]

    def test_delete(self):
        cfg = get_config()
        cfg.memory.enable_repo_memory = True
        tool = MemoryTool()
        result = tool.store("repo", {"content": "to delete", "type": "rule"}, repo_id=1)
        object_id = result["object_id"]
        assert tool.delete("repo", object_id)["deleted"] is True
        assert tool.delete("repo", object_id)["deleted"] is False

    def test_secrets_redacted(self):
        cfg = get_config()
        cfg.memory.enable_repo_memory = True
        tool = MemoryTool()
        tool.store("repo", {
            "content": "api_key=sk-1234567890abcdef secret_token=abc123",
            "type": "rule",
        }, repo_id=1)
        results = tool.query("repo", "api_key", repo_id=1)
        assert "***REDACTED***" in results[0]["content"]
        assert "sk-1234567890abcdef" not in results[0]["content"]

    def test_full_code_block_refused(self):
        cfg = get_config()
        cfg.memory.enable_repo_memory = True
        cfg.memory.store_full_code = False
        tool = MemoryTool()
        long_code = "\n".join(f"    line_{i} = {i}" for i in range(30))
        tool.store("repo", {"content": long_code, "type": "rule"}, repo_id=1)
        results = tool.query("repo", "line", repo_id=1)
        # 完整代码应被截断
        assert len(results) == 1
        assert "完整代码已移除" in results[0]["content"]
