"""LLM 客户端测试（mock 模式）。"""

from __future__ import annotations

import pytest

from app.llm import LLMError, MockLLMClient, get_llm_client


class TestMockLLM:
    def test_returns_json(self):
        client = MockLLMClient()
        resp = client.chat("review plan", "test", json_mode=True)
        data = resp.parse_json()
        assert isinstance(data, dict)

    def test_planner_response(self):
        client = MockLLMClient()
        resp = client.chat("You are a review Planner", "plan this", json_mode=True)
        data = resp.parse_json()
        assert "tasks" in data

    def test_security_response(self):
        client = MockLLMClient()
        resp = client.chat("You are a Security reviewer", "review this", json_mode=True)
        data = resp.parse_json()
        assert "issues" in data

    def test_get_client_returns_mock(self):
        client = get_llm_client()
        assert isinstance(client, MockLLMClient)

    def test_parse_json_with_fences(self):
        from app.llm.client import LLMResponse

        resp = LLMResponse(content='```json\n{"key": "value"}\n```')
        data = resp.parse_json()
        assert data == {"key": "value"}

    def test_parse_json_with_surrounding_text(self):
        from app.llm.client import LLMResponse

        resp = LLMResponse(content='Here is the result: {"issues": []} done')
        data = resp.parse_json()
        assert data == {"issues": []}

    def test_parse_json_invalid_raises(self):
        from app.llm.client import LLMResponse

        resp = LLMResponse(content="not json at all")
        with pytest.raises(LLMError):
            resp.parse_json()
