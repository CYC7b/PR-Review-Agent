"""API v1 汇总路由 —— 挂载在 /api/v1 前缀下的所有资源。"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.reviews import router as reviews_router
from app.api.v1.webhooks import router as webhooks_router

router = APIRouter(prefix="/api/v1")
router.include_router(webhooks_router)
router.include_router(reviews_router)

__all__ = ["router"]
