"""ORM 模型：将 Pydantic 模型持久化到关系数据库。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


class ReviewTaskORM(Base, TimestampMixin):
    __tablename__ = "review_tasks"
    __table_args__ = (
        # 幂等键：repository_id + pr_number + head_sha 唯一（SPEC 4.3）
        UniqueConstraint("repository_id", "pr_number", "head_sha", name="uq_review_task_key"),
        Index("ix_review_task_repo_pr", "repository_id", "pr_number"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    review_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    repository_id: Mapped[int] = mapped_column(Integer, index=True)
    repository_full_name: Mapped[str] = mapped_column(String(256))
    pr_number: Mapped[int] = mapped_column(Integer)
    base_sha: Mapped[str] = mapped_column(String(64))
    head_sha: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="RECEIVED", index=True)
    trigger: Mapped[str] = mapped_column(String(64), default="")
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    issues: Mapped[list[IssueORM]] = relationship(
        back_populates="review", cascade="all, delete-orphan"
    )
    patches: Mapped[list[PatchORM]] = relationship(
        back_populates="review", cascade="all, delete-orphan"
    )
    logs: Mapped[list[ToolCallLogORM]] = relationship(
        back_populates="review", cascade="all, delete-orphan"
    )

    @property
    def task_key(self) -> str:
        return f"{self.repository_id}:{self.pr_number}:{self.head_sha}"


class IssueORM(Base, TimestampMixin):
    __tablename__ = "issues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    issue_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    review_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("review_tasks.review_id", ondelete="CASCADE"), index=True
    )
    type: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(16), index=True)
    confidence: Mapped[float] = mapped_column(default=0.0)
    title: Mapped[str] = mapped_column(String(512))
    file: Mapped[str] = mapped_column(String(512), default="")
    line_start: Mapped[int | None] = mapped_column(nullable=True)
    line_end: Mapped[int | None] = mapped_column(nullable=True)
    introduced_by_pr: Mapped[bool] = mapped_column(Boolean, default=True)
    producer: Mapped[str] = mapped_column(String(64), default="")
    data: Mapped[dict] = mapped_column(JSON, default=dict)  # 完整 issue JSON
    verification_status: Mapped[str] = mapped_column(String(16), default="not_run")
    publication_status: Mapped[str] = mapped_column(String(16), default="pending")
    github_comment_id: Mapped[int | None] = mapped_column(nullable=True)

    review: Mapped[ReviewTaskORM] = relationship(back_populates="issues")


class PatchORM(Base, TimestampMixin):
    __tablename__ = "patches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    patch_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    review_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("review_tasks.review_id", ondelete="CASCADE"), index=True
    )
    issue_ids: Mapped[list] = mapped_column(JSON, default=list)
    strategy: Mapped[str] = mapped_column(String(64), default="minimal_fix")
    diff: Mapped[str] = mapped_column(Text, default="")
    files_changed: Mapped[list] = mapped_column(JSON, default=list)
    risk_level: Mapped[str] = mapped_column(String(16), default="low")
    validation_status: Mapped[str] = mapped_column(String(16), default="not_run")
    publish_mode: Mapped[str] = mapped_column(String(32), default="suppressed")
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    validation: Mapped[dict] = mapped_column(JSON, default=dict)

    review: Mapped[ReviewTaskORM] = relationship(back_populates="patches")


class ToolCallLogORM(Base, TimestampMixin):
    """工具调用审计日志（SPEC 2.1）。"""

    __tablename__ = "tool_call_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    review_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("review_tasks.review_id", ondelete="CASCADE"), index=True
    )
    caller: Mapped[str] = mapped_column(String(128))
    tool: Mapped[str] = mapped_column(String(128))
    input_summary: Mapped[str] = mapped_column(Text, default="")
    output_summary: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(nullable=True)
    head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)

    review: Mapped[ReviewTaskORM] = relationship(back_populates="logs")


class BlackboardORM(Base, TimestampMixin):
    """Blackboard 持久化备份（主存储在 Redis/内存）。"""

    __tablename__ = "blackboard_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    review_id: Mapped[str] = mapped_column(String(128), index=True)
    topic: Mapped[str] = mapped_column(String(128), index=True)
    producer: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class MemoryItemORM(Base, TimestampMixin):
    """长期记忆条目（SPEC 11，默认 repo memory 关闭）。"""

    __tablename__ = "memory_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(16), index=True)
    repo_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    object_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    insight_type: Mapped[str] = mapped_column(String(64), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    extra: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
