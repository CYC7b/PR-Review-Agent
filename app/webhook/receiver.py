"""Webhook Receiver —— 接收 GitHub 事件（SPEC 3.2 / 5.1）。

验证 webhook signature，解析 PR 事件，提交给 Orchestrator。
MVP 支持同步处理；生产环境应改为异步队列。
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.logging_setup import get_logger
from app.models import WebhookEvent
from app.orchestrator import get_orchestrator
from app.tools.github_tool import GitHubClient

logger = get_logger(__name__)

router = APIRouter(prefix="/webhook", tags=["webhook"])

# GitHub 事件类型白名单
_SUPPORTED_EVENTS = {"pull_request"}


@router.post("/github")
async def github_webhook(
    request: Request,
    x_github_event: str | None = Header(None),
    x_github_delivery: str | None = Header(None),
    x_hub_signature_256: str | None = Header(None),
) -> Response:
    """接收 GitHub webhook。"""
    raw_body = await request.body()
    settings = get_settings()
    from app.config import get_config

    require_sig = get_config().github.require_webhook_signature

    # 1. 验证签名（SPEC 5.1 / C5：fail-closed）
    if settings.github_webhook_secret:
        if not GitHubClient.verify_webhook_signature(
            raw_body, x_hub_signature_256, settings.github_webhook_secret
        ):
            logger.warning("webhook.signature_invalid", delivery=x_github_delivery)
            raise HTTPException(status_code=401, detail="Invalid webhook signature")
    elif require_sig:
        # 未配置 secret 且要求校验 → 拒绝处理，绝不 fail-open 接受伪造事件。
        logger.error("webhook.secret_not_configured_reject", delivery=x_github_delivery)
        raise HTTPException(
            status_code=503,
            detail="Webhook signature required but GITHUB_WEBHOOK_SECRET is not configured",
        )
    else:
        logger.warning("webhook.signature_verification_disabled")

    # 2. 事件类型检查
    if x_github_event not in _SUPPORTED_EVENTS:
        return Response(status_code=200, content='{"status":"ignored"}', media_type="application/json")

    # 3. 解析 payload
    import json

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from None

    parsed = GitHubClient.parse_webhook_event(payload)
    if parsed is None:
        return Response(status_code=200, content='{"status":"ignored"}', media_type="application/json")

    action = parsed["action"]
    event_type = parsed["event_type"]
    from app.config import get_config

    if event_type not in get_config().review.trigger_events:
        logger.info("webhook.event_not_in_trigger_list", github_event=event_type)
        return Response(status_code=200, content='{"status":"ignored"}', media_type="application/json")

    # 4. 构造 WebhookEvent
    event = WebhookEvent(
        event_type=event_type,
        action=action,
        repository_id=parsed["repository_id"],
        repository_full_name=parsed["repository_full_name"],
        pr_number=parsed["pr_number"],
        base_sha=parsed["base_sha"],
        head_sha=parsed["head_sha"],
        delivery_id=x_github_delivery or "",
        raw_payload=payload,
    )

    # 5. 提交给 Orchestrator
    orchestrator = get_orchestrator()
    task = orchestrator.handle_event(event)

    # 6. 异步处理（MVP 同步执行，后台任务）
    # 生产环境应将 review_id 入队，由 worker 消费
    asyncio.create_task(_process_review(task.review_id))

    logger.info("webhook.accepted", review_id=task.review_id,
                github_event=event_type, pr=event.repository_full_name)

    return JSONResponse(
        status_code=202,
        content={
            "status": "accepted",
            "review_id": task.review_id,
            "task_status": task.status.value,
        },
    )


async def _process_review(review_id: str) -> None:
    """后台执行审查流水线。"""
    loop = asyncio.get_event_loop()
    orchestrator = get_orchestrator()
    try:
        await loop.run_in_executor(None, orchestrator.process, review_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("webhook.process_failed", review_id=review_id, error=str(exc))


@router.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "service": "pr-review-agent"}


@router.get("/reviews/{review_id}")
async def get_review(review_id: str) -> JSONResponse:
    """查询审查任务状态。"""
    from app.db.repositories import ReviewTaskRepository
    from app.db.session import session_scope

    with session_scope() as session:
        task = ReviewTaskRepository(session).get(review_id)
    if task is None:
        raise HTTPException(status_code=404, detail="review not found")
    return JSONResponse(content={
        "review_id": task.review_id,
        "status": task.status.value,
        "repository": task.repository_full_name,
        "pr_number": task.pr_number,
        "head_sha": task.head_sha[:12],
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "error": task.error,
    })


@router.get("/reviews")
async def list_reviews(limit: int = 20) -> JSONResponse:
    from app.db.repositories import ReviewTaskRepository
    from app.db.session import session_scope

    with session_scope() as session:
        tasks = ReviewTaskRepository(session).list_recent(limit=limit)
    return JSONResponse(content={
        "reviews": [
            {
                "review_id": t.review_id,
                "status": t.status.value,
                "repository": t.repository_full_name,
                "pr_number": t.pr_number,
                "head_sha": t.head_sha[:12],
            }
            for t in tasks
        ]
    })
