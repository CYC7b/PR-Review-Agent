"""GitHub Tool —— PR 元数据、diff、文件内容、review/评论/check（SPEC 8.1）。

支持两种认证方式：
1. GitHub App（私钥 + App ID → installation token）
2. Classic PAT（GITHUB_TOKEN）

遵循最小权限（SPEC 2.3 / 12.1）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Any

import httpx

from app.config import get_settings
from app.logging_setup import get_logger

logger = get_logger(__name__)

GITHUB_API = "https://api.github.com"


class GitHubAuthError(Exception):
    pass


class GitHubAPIError(Exception):
    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(f"GitHub API {status}: {message}")


class GitHubClient:
    """GitHub REST API 客户端。"""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._installation_tokens: dict[int, tuple[str, float]] = {}
        self._client: httpx.Client | None = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=30.0)
        return self._client

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None

    # ---------------- 带限流退避的请求封装（SPEC 15.1 / 12.2 / H4） ----------------

    def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """执行 HTTP 请求；遇到 GitHub 限流（403/429 且 remaining=0）时退避重试。"""
        from app.config import get_config

        max_retries = get_config().github.api_max_retries
        attempt = 0
        while True:
            resp = self.client.request(method, url, **kwargs)
            if resp.status_code in (403, 429) and self._is_rate_limited(resp) and attempt < max_retries:
                wait = self._retry_after_seconds(resp)
                attempt += 1
                logger.warning("github.rate_limited_retry", url=url, attempt=attempt, wait_s=wait)
                time.sleep(wait)
                continue
            return resp

    @staticmethod
    def _is_rate_limited(resp: httpx.Response) -> bool:
        if resp.headers.get("X-RateLimit-Remaining") == "0":
            return True
        return "rate limit" in resp.text.lower() or "secondary rate" in resp.text.lower()

    @staticmethod
    def _retry_after_seconds(resp: httpx.Response, default: float = 5.0, cap: float = 60.0) -> float:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), cap)
            except ValueError:
                pass
        reset = resp.headers.get("X-RateLimit-Reset")
        if reset:
            try:
                delta = float(reset) - time.time()
                if delta > 0:
                    return min(delta, cap)
            except ValueError:
                pass
        return default

    # ---------------- 认证 ----------------

    def _app_jwt(self) -> str:
        """生成 GitHub App 的 JWT（RS256）。"""
        import jwt  # PyJWT

        settings = self._settings
        if not settings.github_app_id:
            raise GitHubAuthError("未配置 GITHUB_APP_ID")
        private_key = self._load_private_key()
        now = int(time.time())
        payload = {
            "iat": now - 60,
            "exp": now + 600,  # 10 分钟
            "iss": settings.github_app_id,
        }
        return jwt.encode(payload, private_key, algorithm="RS256")

    def _load_private_key(self) -> str:
        settings = self._settings
        if settings.github_private_key:
            return settings.github_private_key
        if settings.github_private_key_path:
            from pathlib import Path

            path = Path(settings.github_private_key_path)
            if not path.exists():
                raise GitHubAuthError(f"私钥文件不存在: {path}")
            return path.read_text(encoding="utf-8")
        raise GitHubAuthError("未配置 GitHub App 私钥")

    def _get_installation_id(self, repo_full_name: str, jwt_token: str) -> int:
        """通过仓库获取 installation id。"""
        resp = self.client.get(
            f"{GITHUB_API}/repos/{repo_full_name}/installation",
            headers={"Authorization": f"Bearer {jwt_token}", "Accept": "application/vnd.github+json"},
        )
        if resp.status_code != 200:
            raise GitHubAPIError(resp.status_code, resp.text)
        return resp.json()["id"]

    def _get_installation_token(self, repo_full_name: str) -> str:
        settings = self._settings
        # PAT 模式
        if settings.github_token:
            return settings.github_token
        # App 模式
        jwt_token = self._app_jwt()
        installation_id = self._get_installation_id(repo_full_name, jwt_token)
        cached = self._installation_tokens.get(installation_id)
        if cached and cached[1] > time.time() + 60:
            return cached[0]
        resp = self.client.post(
            f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {jwt_token}", "Accept": "application/vnd.github+json"},
        )
        if resp.status_code != 201:
            raise GitHubAPIError(resp.status_code, resp.text)
        token = resp.json()["token"]
        expires_at = time.time() + 3600  # installation token 1 小时有效
        self._installation_tokens[installation_id] = (token, expires_at)
        return token

    def _headers(self, repo_full_name: str) -> dict[str, str]:
        token = self._get_installation_token(repo_full_name)
        return {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    @staticmethod
    def verify_webhook_signature(payload: bytes, signature: str | None, secret: str) -> bool:
        """验证 GitHub webhook HMAC 签名（SPEC 5.1）。"""
        if not secret:
            return False
        if not signature or not signature.startswith("sha256="):
            return False
        expected = "sha256=" + hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    # ---------------- 工具方法（SPEC 8.1） ----------------

    def get_pr(self, repo: str, pr_number: int) -> dict[str, Any]:
        """github.get_pr —— 获取 PR metadata。"""
        resp = self._send(
            "GET",
            f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}",
            headers=self._headers(repo),
        )
        self._raise_for_status(resp)
        data = resp.json()
        return {
            "number": data["number"],
            "title": data.get("title", ""),
            "body": data.get("body", ""),
            "state": data.get("state", ""),
            "head_sha": data["head"]["sha"],
            "base_sha": data["base"]["sha"],
            "head_ref": data["head"]["ref"],
            "base_ref": data["base"]["ref"],
            "user": data.get("user", {}).get("login", ""),
            "draft": data.get("draft", False),
            "changed_files": data.get("changed_files", 0),
            "additions": data.get("additions", 0),
            "deletions": data.get("deletions", 0),
        }

    def get_changed_files(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        """github.get_changed_files —— 获取变更文件列表。"""
        files: list[dict[str, Any]] = []
        page = 1
        while True:
            resp = self._send(
                "GET",
                f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/files",
                params={"per_page": 100, "page": page},
                headers=self._headers(repo),
            )
            self._raise_for_status(resp)
            batch = resp.json()
            if not batch:
                break
            for f in batch:
                files.append({
                    "path": f["filename"],
                    "status": f["status"],  # added|removed|modified|renamed
                    "additions": f.get("additions", 0),
                    "deletions": f.get("deletions", 0),
                    "changes": f.get("changes", 0),
                    "patch": f.get("patch", ""),
                    "sha": f.get("sha", ""),
                })
            if len(batch) < 100:
                break
            page += 1
        return files

    def get_diff(self, repo: str, pr_number: int, files_filter: list[str] | None = None) -> str:
        """github.get_diff —— 获取 unified diff（可按文件过滤）。"""
        if files_filter:
            # 逐文件拼接 patch
            all_files = self.get_changed_files(repo, pr_number)
            wanted = set(files_filter)
            parts = []
            for f in all_files:
                if f["path"] in wanted and f.get("patch"):
                    parts.append(f["patch"])
            return "\n".join(parts)
        # 完整 diff
        resp = self._send(
            "GET",
            f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}",
            headers={**self._headers(repo), "Accept": "application/vnd.github.v3.diff"},
        )
        self._raise_for_status(resp)
        return resp.text

    def get_file_content(self, repo: str, ref: str, path: str) -> str:
        """github.get_file_content —— 获取文件内容。"""
        resp = self._send(
            "GET",
            f"{GITHUB_API}/repos/{repo}/contents/{path}",
            params={"ref": ref},
            headers=self._headers(repo),
        )
        self._raise_for_status(resp)
        data = resp.json()
        if data.get("encoding") == "base64":
            return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        return data.get("content", "")

    def create_review(
        self,
        repo: str,
        pr_number: int,
        head_sha: str,
        comments: list[dict[str, Any]],
        body: str = "",
        event: str = "COMMENT",
    ) -> dict[str, Any]:
        """github.create_review —— 创建 PR review（绑定 head SHA）。

        comments: [{"path": ..., "line": ..., "body": ..., "side": "RIGHT", "start_line": ...}]
        """
        payload: dict[str, Any] = {
            "commit_id": head_sha,
            "event": event,
            "comments": comments,
        }
        if body:
            payload["body"] = body
        resp = self._send(
            "POST",
            f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/reviews",
            json=payload,
            headers=self._headers(repo),
        )
        self._raise_for_status(resp)
        data = resp.json()
        return {"review_id": data.get("id"), "state": data.get("state")}

    def create_comment(self, repo: str, pr_number: int, body: str) -> dict[str, Any]:
        """github.create_comment —— 发布 PR issue comment。"""
        resp = self._send(
            "POST",
            f"{GITHUB_API}/repos/{repo}/issues/{pr_number}/comments",
            json={"body": body},
            headers=self._headers(repo),
        )
        self._raise_for_status(resp)
        data = resp.json()
        return {"comment_id": data.get("id")}

    def update_comment(self, repo: str, comment_id: int, body: str) -> dict[str, Any]:
        """github.update_comment —— 更新评论。"""
        resp = self._send(
            "PATCH",
            f"{GITHUB_API}/repos/{repo}/issues/comments/{comment_id}",
            json={"body": body},
            headers=self._headers(repo),
        )
        self._raise_for_status(resp)
        return {"updated": True}

    def create_check_run(
        self, repo: str, head_sha: str, conclusion: str, summary: str, title: str = "PR Review Agent"
    ) -> dict[str, Any]:
        """github.create_check_run —— 发布 check run。"""
        resp = self._send(
            "POST",
            f"{GITHUB_API}/repos/{repo}/check-runs",
            json={
                "name": title,
                "head_sha": head_sha,
                "status": "completed",
                "conclusion": conclusion,
                "output": {"title": title, "summary": summary},
            },
            headers=self._headers(repo),
        )
        self._raise_for_status(resp)
        data = resp.json()
        return {"check_run_id": data.get("id")}

    # ---------------- 辅助 ----------------

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if resp.status_code >= 400:
            raise GitHubAPIError(resp.status_code, resp.text[:500])

    @staticmethod
    def parse_webhook_event(payload: dict) -> dict[str, Any] | None:
        """从 webhook payload 解析 PR 事件（SPEC 5.1）。

        返回 None 表示非关注事件。
        """
        if "pull_request" not in payload:
            return None
        action = payload.get("action", "")
        pr = payload["pull_request"]
        repo = payload.get("repository", {})
        return {
            "event_type": f"pull_request.{action}",
            "action": action,
            "repository_id": repo.get("id", 0),
            "repository_full_name": repo.get("full_name", ""),
            "pr_number": pr.get("number", 0),
            "base_sha": pr.get("base", {}).get("sha", ""),
            "head_sha": pr.get("head", {}).get("sha", ""),
        }


_default_client: GitHubClient | None = None


def get_github_client() -> GitHubClient:
    global _default_client
    if _default_client is None:
        _default_client = GitHubClient()
    return _default_client


def reset_github_client() -> None:
    global _default_client
    if _default_client:
        _default_client.close()
    _default_client = None
