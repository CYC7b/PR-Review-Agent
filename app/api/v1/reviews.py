"""Review 任务查询（资源：reviews）。

所有端点要求 Authorization: Bearer <API_KEY>（fail-closed，见 app.api.deps）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import require_api_key
from app.api.schemas import ErrorResponse, ReviewDetail, ReviewListResponse, ReviewSummary
from app.db.repositories import ReviewTaskRepository
from app.db.session import session_scope

router = APIRouter(
    prefix="/reviews",
    tags=["reviews"],
    dependencies=[Depends(require_api_key)],
    responses={
        401: {"model": ErrorResponse, "description": "缺失或无效的 Bearer token"},
        503: {"model": ErrorResponse, "description": "未配置 API_KEY（fail-closed，拒绝处理）"},
    },
)


@router.get("", response_model=ReviewListResponse)
async def list_reviews(limit: int = Query(20, ge=1, le=100)) -> ReviewListResponse:
    """分页列出最近的审查任务。"""
    with session_scope() as session:
        tasks = ReviewTaskRepository(session).list_recent(limit=limit)
    summaries = [
        ReviewSummary(
            review_id=t.review_id,
            status=t.status,
            repository=t.repository_full_name,
            pr_number=t.pr_number,
            head_sha=t.head_sha[:12],
        )
        for t in tasks
    ]
    return ReviewListResponse(reviews=summaries, limit=limit, count=len(summaries))


@router.get(
    "/{review_id}",
    response_model=ReviewDetail,
    responses={404: {"model": ErrorResponse, "description": "任务不存在"}},
)
async def get_review(review_id: str) -> ReviewDetail:
    """查询单个审查任务详情。"""
    with session_scope() as session:
        task = ReviewTaskRepository(session).get(review_id)
    if task is None:
        raise HTTPException(status_code=404, detail="review not found")
    return ReviewDetail(
        review_id=task.review_id,
        status=task.status,
        repository=task.repository_full_name,
        pr_number=task.pr_number,
        head_sha=task.head_sha[:12],
        created_at=task.created_at,
        completed_at=task.completed_at,
        error=task.error,
    )
