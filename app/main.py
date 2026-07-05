"""FastAPI 应用入口 —— 组装 Webhook Receiver 与管理端点。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.db.session import init_db
from app.logging_setup import configure_logging, get_logger
from app.tools import register_tools
from app.webhook.receiver import router as webhook_router

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.app_log_level)
    logger.info("app.starting", env=settings.app_env, port=settings.app_port)
    init_db()
    register_tools()
    logger.info("app.started")
    yield
    logger.info("app.stopping")


def create_app() -> FastAPI:
    app = FastAPI(
        title="PR Review Agent",
        description="证据化、可验证的自动化 PR 审查系统（Sandbox Execution）",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(webhook_router)
    return app


app = create_app()
