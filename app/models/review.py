"""Review Task 与任务配置模型（SPEC 6.1）。"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from app.models.common import ReviewState


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ReviewTaskConfig(BaseModel):
    auto_patch: bool = False
    max_parallel_agents: int = 4
    sandbox_timeout_seconds: int = 1800


class ReviewTask(BaseModel):
    """一次 PR 审查任务。"""

    review_id: str
    repository_id: int
    repository_full_name: str
    pr_number: int
    base_sha: str
    head_sha: str
    status: ReviewState = ReviewState.RECEIVED
    trigger: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    config: ReviewTaskConfig = Field(default_factory=ReviewTaskConfig)
    error: str | None = None

    # 任务键（幂等）：repository_id + pr_number + head_sha
    @property
    def task_key(self) -> str:
        return f"{self.repository_id}:{self.pr_number}:{self.head_sha}"

    def touch(self) -> None:
        self.updated_at = utcnow()

    def transition(self, new_state: ReviewState) -> None:
        from app.models.common import TERMINAL_STATES

        if self.status in TERMINAL_STATES:
            raise ValueError(f"任务已处于终态 {self.status.value}，不可流转到 {new_state.value}")
        # 离开 RECEIVED 即视为审查开始
        if self.status == ReviewState.RECEIVED and self.started_at is None:
            self.started_at = utcnow()
        self.status = new_state
        self.touch()
        if new_state == ReviewState.COMPLETED or new_state in {
            ReviewState.FAILED,
            ReviewState.CANCELLED,
            ReviewState.SUPERSEDED,
            ReviewState.TIMEOUT,
            ReviewState.SKIPPED,
        }:
            self.completed_at = utcnow()


class WebhookEvent(BaseModel):
    """解析后的 GitHub webhook 事件。"""

    event_type: str  # e.g. pull_request.opened
    action: str  # opened | synchronize | reopened
    repository_id: int
    repository_full_name: str
    pr_number: int
    base_sha: str
    head_sha: str
    delivery_id: str = ""
    raw_payload: dict | None = None
