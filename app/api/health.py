"""健康检查 —— 无版本前缀、无鉴权，供负载均衡器/编排系统探活。"""

from __future__ import annotations

from fastapi import APIRouter

from app import __version__
from app.api.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", service="pr-review-agent", version=__version__)
