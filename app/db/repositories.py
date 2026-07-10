"""仓储层：封装 ORM 查询与持久化，Pydantic ↔ ORM 转换。"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    BlackboardORM,
    IssueORM,
    MemoryItemORM,
    PatchORM,
    ReviewTaskORM,
    ToolCallLogORM,
)
from app.models import (
    BlackboardMessage,
    Issue,
    MemoryScope,
    Patch,
    ReviewState,
    ReviewTask,
    ReviewTaskConfig,
    TestRunReport,
)

# --------------------------- ReviewTask ---------------------------

class ReviewTaskRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def _to_orm(self, task: ReviewTask, orm: ReviewTaskORM | None = None) -> ReviewTaskORM:
        orm = orm or ReviewTaskORM()
        orm.review_id = task.review_id
        orm.repository_id = task.repository_id
        orm.repository_full_name = task.repository_full_name
        orm.pr_number = task.pr_number
        orm.base_sha = task.base_sha
        orm.head_sha = task.head_sha
        orm.status = task.status.value
        orm.trigger = task.trigger
        orm.config = task.config.model_dump()
        orm.started_at = task.started_at
        orm.completed_at = task.completed_at
        orm.error = task.error
        orm.summary = task.test_report.model_dump_json() if task.test_report else None
        return orm

    def _to_pydantic(self, orm: ReviewTaskORM) -> ReviewTask:
        test_report = None
        if orm.summary:
            try:
                test_report = TestRunReport.model_validate_json(orm.summary)
            except (ValueError, TypeError):
                pass
        return ReviewTask(
            review_id=orm.review_id,
            repository_id=orm.repository_id,
            repository_full_name=orm.repository_full_name,
            pr_number=orm.pr_number,
            base_sha=orm.base_sha,
            head_sha=orm.head_sha,
            status=ReviewState(orm.status),
            trigger=orm.trigger,
            created_at=orm.created_at,
            updated_at=orm.updated_at,
            started_at=orm.started_at,
            completed_at=orm.completed_at,
            config=ReviewTaskConfig(**orm.config) if orm.config else ReviewTaskConfig(),
            error=orm.error,
            test_report=test_report,
        )

    def save(self, task: ReviewTask) -> ReviewTask:
        orm = self.session.get(ReviewTaskORM, task.review_id) if False else None
        # 用 review_id 唯一键查询
        stmt = select(ReviewTaskORM).where(ReviewTaskORM.review_id == task.review_id)
        orm = self.session.execute(stmt).scalar_one_or_none()
        orm = self._to_orm(task, orm)
        if orm.id is None:
            self.session.add(orm)
        self.session.flush()
        return self._to_pydantic(orm)

    def get(self, review_id: str) -> ReviewTask | None:
        orm = self.session.execute(
            select(ReviewTaskORM).where(ReviewTaskORM.review_id == review_id)
        ).scalar_one_or_none()
        return self._to_pydantic(orm) if orm else None

    def get_by_task_key(self, repository_id: int, pr_number: int, head_sha: str) -> ReviewTask | None:
        orm = self.session.execute(
            select(ReviewTaskORM).where(
                ReviewTaskORM.repository_id == repository_id,
                ReviewTaskORM.pr_number == pr_number,
                ReviewTaskORM.head_sha == head_sha,
            )
        ).scalar_one_or_none()
        return self._to_pydantic(orm) if orm else None

    def find_active_for_pr(self, repository_id: int, pr_number: int) -> list[ReviewTask]:
        """查找某 PR 下未进入终态的任务（用于 SUPERSEDED 取消）。"""
        active_states = {
            ReviewState.RECEIVED.value,
            ReviewState.VALIDATING_EVENT.value,
            ReviewState.PREPROCESSING.value,
            ReviewState.PLANNING.value,
            ReviewState.SANDBOX_PREPARE.value,
            ReviewState.STATIC_REVIEW.value,
            ReviewState.DYNAMIC_TESTING.value,
            ReviewState.ISSUE_AGGREGATION.value,
            ReviewState.PATCH_GENERATION.value,
            ReviewState.PATCH_VALIDATION.value,
            ReviewState.REVIEW_PUBLISHING.value,
        }
        rows = self.session.execute(
            select(ReviewTaskORM).where(
                ReviewTaskORM.repository_id == repository_id,
                ReviewTaskORM.pr_number == pr_number,
                ReviewTaskORM.status.in_(active_states),
            )
        ).scalars().all()
        return [self._to_pydantic(r) for r in rows]

    def list_recent(self, limit: int = 50) -> list[ReviewTask]:
        rows = self.session.execute(
            select(ReviewTaskORM).order_by(ReviewTaskORM.created_at.desc()).limit(limit)
        ).scalars().all()
        return [self._to_pydantic(r) for r in rows]


# --------------------------- Issue ---------------------------

class IssueRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def save(self, issue: Issue) -> Issue:
        orm = self.session.execute(
            select(IssueORM).where(IssueORM.issue_id == issue.id)
        ).scalar_one_or_none()
        if orm is None:
            orm = IssueORM(issue_id=issue.id)
            self.session.add(orm)
        orm.review_id = issue.review_id
        orm.type = issue.type.value
        orm.severity = issue.severity.value
        orm.confidence = issue.confidence
        orm.title = issue.title
        orm.file = issue.file
        orm.line_start = issue.line_start
        orm.line_end = issue.line_end
        orm.introduced_by_pr = issue.introduced_by_pr
        orm.producer = issue.producer
        orm.data = json.loads(issue.model_dump_json())
        orm.verification_status = issue.verification.status.value
        orm.publication_status = issue.publication.status.value
        orm.github_comment_id = issue.publication.github_comment_id
        self.session.flush()
        return issue

    def list_for_review(self, review_id: str) -> list[Issue]:
        rows = self.session.execute(
            select(IssueORM).where(IssueORM.review_id == review_id)
        ).scalars().all()
        return [Issue(**r.data) for r in rows if r.data]

    def get(self, issue_id: str) -> Issue | None:
        orm = self.session.execute(
            select(IssueORM).where(IssueORM.issue_id == issue_id)
        ).scalar_one_or_none()
        return Issue(**orm.data) if orm and orm.data else None


# --------------------------- Patch ---------------------------

class PatchRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def save(self, patch: Patch) -> Patch:
        orm = self.session.execute(
            select(PatchORM).where(PatchORM.patch_id == patch.patch_id)
        ).scalar_one_or_none()
        if orm is None:
            orm = PatchORM(patch_id=patch.patch_id)
            self.session.add(orm)
        orm.review_id = patch.review_id
        orm.issue_ids = patch.issue_ids
        orm.strategy = patch.strategy
        orm.diff = patch.diff
        orm.files_changed = patch.files_changed
        orm.risk_level = patch.risk_level
        orm.validation_status = patch.validation.status.value
        orm.publish_mode = patch.publish_mode.value
        orm.retry_count = patch.retry_count
        orm.validation = json.loads(patch.validation.model_dump_json())
        self.session.flush()
        return patch

    def list_for_review(self, review_id: str) -> list[Patch]:
        rows = self.session.execute(
            select(PatchORM).where(PatchORM.review_id == review_id)
        ).scalars().all()
        result = []
        for r in rows:
            p = Patch(
                patch_id=r.patch_id,
                issue_ids=r.issue_ids,
                review_id=r.review_id,
                strategy=r.strategy,
                diff=r.diff,
                files_changed=r.files_changed,
                risk_level=r.risk_level,
                validation_status=r.validation_status,
                publish_mode=r.publish_mode,
                retry_count=r.retry_count,
            )
            result.append(p)
        return result


# --------------------------- ToolCallLog ---------------------------

class ToolCallLogRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def log(
        self,
        review_id: str,
        caller: str,
        tool: str,
        input_summary: str = "",
        output_summary: str = "",
        error: str | None = None,
        duration_ms: int | None = None,
        head_sha: str | None = None,
    ) -> None:
        orm = ToolCallLogORM(
            review_id=review_id,
            caller=caller,
            tool=tool,
            input_summary=input_summary,
            output_summary=output_summary,
            error=error,
            duration_ms=duration_ms,
            head_sha=head_sha,
        )
        self.session.add(orm)
        self.session.flush()


# --------------------------- Blackboard ---------------------------

class BlackboardRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def append(self, msg: BlackboardMessage) -> None:
        orm = BlackboardORM(
            review_id=msg.review_id,
            topic=msg.topic,
            producer=msg.producer,
            payload=msg.payload,
        )
        self.session.add(orm)
        self.session.flush()

    def list_for_review(self, review_id: str, topic: str | None = None) -> list[BlackboardMessage]:
        stmt = select(BlackboardORM).where(BlackboardORM.review_id == review_id)
        if topic:
            stmt = stmt.where(BlackboardORM.topic == topic)
        rows = self.session.execute(stmt).scalars().all()
        return [
            BlackboardMessage(
                topic=r.topic,
                review_id=r.review_id,
                producer=r.producer,
                timestamp=r.created_at,
                payload=r.payload,
            )
            for r in rows
        ]


# --------------------------- Memory ---------------------------

class MemoryRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def store(self, scope: MemoryScope, object_id: str, content: str,
              insight_type: str = "", repo_id: int | None = None,
              metadata: dict | None = None, expires_at: Any = None) -> None:
        orm = self.session.execute(
            select(MemoryItemORM).where(MemoryItemORM.object_id == object_id)
        ).scalar_one_or_none()
        if orm is None:
            orm = MemoryItemORM(object_id=object_id)
            self.session.add(orm)
        orm.scope = scope.value
        orm.repo_id = repo_id
        orm.insight_type = insight_type
        orm.content = content
        orm.extra = metadata or {}
        orm.expires_at = expires_at
        self.session.flush()

    def query(self, scope: MemoryScope, query: str, repo_id: int | None = None,
              limit: int = 20) -> list[dict]:
        stmt = select(MemoryItemORM).where(MemoryItemORM.scope == scope.value)
        if scope == MemoryScope.REPO and repo_id is not None:
            stmt = stmt.where(MemoryItemORM.repo_id == repo_id)
        # 简单子串匹配（MVP）。V1 接入向量检索。
        if query:
            stmt = stmt.where(MemoryItemORM.content.ilike(f"%{query}%"))
        rows = self.session.execute(stmt.limit(limit)).scalars().all()
        return [{"object_id": r.object_id, "content": r.content, "type": r.insight_type} for r in rows]

    def delete(self, scope: MemoryScope, object_id: str) -> bool:
        orm = self.session.execute(
            select(MemoryItemORM).where(MemoryItemORM.object_id == object_id)
        ).scalar_one_or_none()
        if orm is None:
            return False
        self.session.delete(orm)
        self.session.flush()
        return True
