"""API v1 请求/响应模型 —— 这些模型即 REST Contract 的 schema 来源。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.models import ReviewState


class HealthResponse(BaseModel):
    status: str = Field(examples=["ok"])
    service: str
    version: str


class WebhookAcceptedResponse(BaseModel):
    status: str = Field("accepted", examples=["accepted"])
    review_id: str
    task_status: ReviewState


class WebhookIgnoredResponse(BaseModel):
    status: str = Field("ignored", examples=["ignored"])


class ReviewSummary(BaseModel):
    review_id: str
    status: ReviewState
    repository: str
    pr_number: int
    head_sha: str


class ReviewDetail(ReviewSummary):
    created_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None


class ReviewListResponse(BaseModel):
    reviews: list[ReviewSummary]
    limit: int
    count: int


class ErrorResponse(BaseModel):
    detail: str
