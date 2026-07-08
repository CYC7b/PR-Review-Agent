"""共享 API 依赖：管理类端点的 Bearer token 鉴权。

策略与 webhook 签名校验一致（fail-closed）：若要求鉴权但未配置 API_KEY，
直接拒绝服务，绝不退化为放行。
"""

from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_config, get_settings

_bearer_scheme = HTTPBearer(auto_error=False)


async def require_api_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> None:
    settings = get_settings()
    require_auth = get_config().api.require_api_key

    if not settings.api_key:
        if require_auth:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="API access requires authentication but API_KEY is not configured",
            )
        return

    if credentials is None or not secrets.compare_digest(
        credentials.credentials.encode("utf-8"), settings.api_key.encode("utf-8")
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API token",
            headers={"WWW-Authenticate": "Bearer"},
        )
