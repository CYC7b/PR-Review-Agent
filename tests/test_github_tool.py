"""GitHub 工具测试（SPEC 8.1 / 5.1）。"""

from __future__ import annotations

import hashlib
import hmac

from app.tools.github_tool import GitHubClient


class TestWebhookSignature:
    def test_valid_signature(self):
        secret = "my-secret"
        payload = b'{"action":"opened"}'
        signature = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        assert GitHubClient.verify_webhook_signature(payload, signature, secret) is True

    def test_invalid_signature(self):
        secret = "my-secret"
        payload = b'{"action":"opened"}'
        signature = "sha256=invalid"
        assert GitHubClient.verify_webhook_signature(payload, signature, secret) is False

    def test_missing_signature(self):
        assert GitHubClient.verify_webhook_signature(b"data", None, "secret") is False

    def test_missing_secret(self):
        assert GitHubClient.verify_webhook_signature(b"data", "sha256=abc", "") is False


class TestEventParsing:
    def test_parse_pull_request_event(self):
        payload = {
            "action": "opened",
            "repository": {"id": 123, "full_name": "org/repo"},
            "pull_request": {
                "number": 45,
                "head": {"sha": "abc123"},
                "base": {"sha": "base123"},
            },
        }
        result = GitHubClient.parse_webhook_event(payload)
        assert result is not None
        assert result["event_type"] == "pull_request.opened"
        assert result["action"] == "opened"
        assert result["repository_id"] == 123
        assert result["repository_full_name"] == "org/repo"
        assert result["pr_number"] == 45
        assert result["head_sha"] == "abc123"
        assert result["base_sha"] == "base123"

    def test_parse_non_pr_event(self):
        payload = {"action": "created", "issue": {"number": 1}}
        result = GitHubClient.parse_webhook_event(payload)
        assert result is None

    def test_parse_synchronize_event(self):
        payload = {
            "action": "synchronize",
            "repository": {"id": 1, "full_name": "o/r"},
            "pull_request": {"number": 2, "head": {"sha": "new"}, "base": {"sha": "old"}},
        }
        result = GitHubClient.parse_webhook_event(payload)
        assert result["event_type"] == "pull_request.synchronize"
