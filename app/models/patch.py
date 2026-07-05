"""Patch Schema（SPEC 6.3）。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.models.common import PatchPublishMode, VerificationStatus


class PatchValidation(BaseModel):
    status: VerificationStatus = VerificationStatus.NOT_RUN
    commands: list[str] = Field(default_factory=list)
    summary: str = ""
    stdout: str | None = None
    stderr: str | None = None


class Patch(BaseModel):
    """针对一个或多个 issue 的修复补丁。"""

    patch_id: str
    issue_ids: list[str] = Field(default_factory=list)
    review_id: str
    strategy: str = "minimal_fix"
    diff: str = ""
    files_changed: list[str] = Field(default_factory=list)
    risk_level: str = "low"
    validation: PatchValidation = Field(default_factory=PatchValidation)
    publish_mode: PatchPublishMode = PatchPublishMode.SUPPRESSED
    retry_count: int = 0

    def is_publishable_as_suggestion(self) -> bool:
        """只有验证通过的 patch 才能作为 GitHub suggestion 发布（SPEC 10.3）。"""
        return self.validation.status == VerificationStatus.PASSED
