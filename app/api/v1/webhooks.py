"""GitHub Webhook 接收（资源：webhooks）。

验证 webhook signature，解析 PR 事件，提交给 Orchestrator。
MVP 支持同步处理；生产环境应改为异步队列。
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from app.api.schemas import ErrorResponse, WebhookAcceptedResponse, WebhookIgnoredResponse
from app.config import get_config, get_settings
from app.logging_setup import get_logger
from app.models import WebhookEvent
from app.orchestrator import get_orchestrator
from app.tools.github_tool import GitHubClient

logger = get_logger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# GitHub 事件类型白名单
_SUPPORTED_EVENTS = {"pull_request"}


@router.post(
    "/github",
    response_model=WebhookAcceptedResponse,
    status_code=202,
    responses={
        200: {"model": WebhookIgnoredResponse, "description": "事件被忽略（非关注事件类型/action）"},
        400: {"model": ErrorResponse, "description": "payload 不是合法 JSON"},
        401: {"model": ErrorResponse, "description": "webhook 签名校验失败"},
        503: {"model": ErrorResponse, "description": "未配置签名密钥（fail-closed，拒绝处理）"},
    },
)
async def receive_github_webhook(
    request: Request,
    response: Response,
    x_github_event: str | None = Header(None),
    x_github_delivery: str | None = Header(None),
    x_hub_signature_256: str | None = Header(None),
) -> WebhookAcceptedResponse | JSONResponse:
    """接收 GitHub webhook 事件，创建一次 PR 审查任务（异步处理）。"""
    raw_body = await request.body()
    settings = get_settings()
    require_sig = get_config().github.require_webhook_signature

    # 1. 验证签名（fail-closed）
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
        return JSONResponse(status_code=200, content={"status": "ignored"})

    # 3. 解析 payload
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from None

    parsed = GitHubClient.parse_webhook_event(payload)
    if parsed is None:
        return JSONResponse(status_code=200, content={"status": "ignored"})

    action = parsed["action"]
    event_type = parsed["event_type"]
    if event_type not in get_config().review.trigger_events:
        logger.info("webhook.event_not_in_trigger_list", github_event=event_type)
        return JSONResponse(status_code=200, content={"status": "ignored"})

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

    response.headers["Location"] = f"/api/v1/reviews/{task.review_id}"
    return WebhookAcceptedResponse(review_id=task.review_id, task_status=task.status)


async def _process_review(review_id: str) -> None:
    """后台执行审查流水线。"""
    loop = asyncio.get_event_loop()
    orchestrator = get_orchestrator()
    try:
        await loop.run_in_executor(None, orchestrator.process, review_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("webhook.process_failed", review_id=review_id, error=str(exc))
