"""审查计划模型（SPEC 5.2）。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.models.common import ChangeType, RiskLevel


class PlannedFile(BaseModel):
    path: str
    change_type: ChangeType = ChangeType.MODIFIED
    risk_level: RiskLevel = RiskLevel.LOW
    reasons: list[str] = Field(default_factory=list)


class PlannedTask(BaseModel):
    id: str
    agent: str
    target_files: list[str] = Field(default_factory=list)
    focus: list[str] = Field(default_factory=list)
    priority: str = "medium"  # high | medium | low
    depends_on: list[str] = Field(default_factory=list)


class ReviewPlan(BaseModel):
    review_id: str
    head_sha: str
    base_sha: str | None = None
    language_profile: list[str] = Field(default_factory=list)
    change_categories: list[str] = Field(default_factory=list)
    changed_files: list[PlannedFile] = Field(default_factory=list)
    tasks: list[PlannedTask] = Field(default_factory=list)
    sandbox_required: bool = False
    estimated_cost_class: str = "low"  # low | medium | high
    skip_reason: str | None = None  # 当 SKIPPED 时填写
