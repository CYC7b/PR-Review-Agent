"""Webhook Receiver 集成测试（SPEC 5.1）。"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from app.main import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c


def _make_pr_payload(action="opened", head_sha="abc123def456", base_sha="base123"):
    return {
        "action": action,
        "repository": {"id": 999, "full_name": "org/test-repo"},
        "pull_request": {
            "number": 42,
            "head": {"sha": head_sha, "ref": "feature"},
            "base": {"sha": base_sha, "ref": "main"},
            "title": "Test PR",
            "body": "Test description",
        },
    }


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class TestWebhookReceiver:
    def test_health(self, client):
        resp = client.get("/webhook/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_missing_secret_rejected(self, client):
        """未配置 secret 且要求签名 → fail-closed 拒绝（SPEC 5.1 / C5）。"""
        payload = _make_pr_payload()
        resp = client.post(
            "/webhook/github",
            data=json.dumps(payload),
            headers={"x-github-event": "pull_request", "content-type": "application/json"},
        )
        assert resp.status_code == 503

    def test_ignore_non_pr_event(self, client, monkeypatch):
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "my-secret")
        from app.config import reload_config

        reload_config()
        body = json.dumps({"action": "created", "issue": {"number": 1}}).encode()
        resp = client.post(
            "/webhook/github",
            data=body,
            headers={"x-github-event": "issue",
                     "x-hub-signature-256": _sign(body, "my-secret")},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"

    def test_accept_pr_event(self, client, monkeypatch):
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "my-secret")
        from app.config import reload_config

        reload_config()
        # mock orchestrator 的 handle_event 避免真实 GitHub 调用
        from app.models import ReviewState, ReviewTask
        from app.orchestrator import orchestrator as orch_module

        def fake_handle_event(self, event):
            return ReviewTask(
                review_id="repo-999-pr-42-sha-abc123def",
                repository_id=999,
                repository_full_name="org/test-repo",
                pr_number=42,
                base_sha="base123",
                head_sha="abc123def456",
                status=ReviewState.RECEIVED,
            )

        monkeypatch.setattr(orch_module.Orchestrator, "handle_event", fake_handle_event)

        body = json.dumps(_make_pr_payload()).encode()
        resp = client.post(
            "/webhook/github",
            data=body,
            headers={"x-github-event": "pull_request", "content-type": "application/json",
                     "x-hub-signature-256": _sign(body, "my-secret")},
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "accepted"
        assert "review_id" in data

    def test_invalid_signature_rejected(self, client, monkeypatch):
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "my-secret")
        from app.config import reload_config

        reload_config()

        payload = _make_pr_payload()
        resp = client.post(
            "/webhook/github",
            data=json.dumps(payload),
            headers={
                "x-github-event": "pull_request",
                "x-hub-signature-256": "sha256=invalid",
                "content-type": "application/json",
            },
        )
        assert resp.status_code == 401

    def test_unsupported_action_ignored(self, client, monkeypatch):
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "my-secret")
        from app.config import reload_config

        reload_config()

        body = json.dumps(_make_pr_payload(action="closed")).encode()
        resp = client.post(
            "/webhook/github",
            data=body,
            headers={"x-github-event": "pull_request", "content-type": "application/json",
                     "x-hub-signature-256": _sign(body, "my-secret")},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"

    def test_get_review_not_found(self, client):
        resp = client.get("/webhook/reviews/nonexistent")
        assert resp.status_code == 404

    def test_list_reviews(self, client):
        resp = client.get("/webhook/reviews")
        assert resp.status_code == 200
        assert "reviews" in resp.json()
