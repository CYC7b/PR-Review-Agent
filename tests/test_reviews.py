"""Review 查询端点鉴权与行为测试。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from app.main import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c


class TestReviewsAuth:
    def test_missing_api_key_rejected_fail_closed(self, client):
        """未配置 API_KEY 且要求鉴权 → fail-closed 拒绝，不退化为放行。"""
        resp = client.get("/api/v1/reviews")
        assert resp.status_code == 503

    def test_no_token_rejected(self, client, monkeypatch):
        monkeypatch.setenv("API_KEY", "secret-token")
        from app.config import reload_config

        reload_config()
        resp = client.get("/api/v1/reviews")
        assert resp.status_code == 401

    def test_invalid_token_rejected(self, client, monkeypatch):
        monkeypatch.setenv("API_KEY", "secret-token")
        from app.config import reload_config

        reload_config()
        resp = client.get("/api/v1/reviews", headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 401

    def test_valid_token_lists_reviews(self, client, monkeypatch):
        monkeypatch.setenv("API_KEY", "secret-token")
        from app.config import reload_config

        reload_config()
        resp = client.get("/api/v1/reviews", headers={"Authorization": "Bearer secret-token"})
        assert resp.status_code == 200
        assert "reviews" in resp.json()

    def test_get_review_not_found(self, client, monkeypatch):
        monkeypatch.setenv("API_KEY", "secret-token")
        from app.config import reload_config

        reload_config()
        resp = client.get(
            "/api/v1/reviews/nonexistent",
            headers={"Authorization": "Bearer secret-token"},
        )
        assert resp.status_code == 404
