"""Issue Schema（SPEC 6.2）。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.models.common import IssueType, PublicationStatus, Severity, VerificationStatus


class ToolOutput(BaseModel):
    tool: str
    summary: str = ""
    raw: str | None = None


class RuntimeOutput(BaseModel):
    command: str
    exit_code: int | None = None
    summary: str = ""
    stdout: str | None = None
    stderr: str | None = None


class IssueEvidence(BaseModel):
    diff_hunk: str = ""
    related_code: str = ""
    tool_outputs: list[ToolOutput] = Field(default_factory=list)
    runtime_outputs: list[RuntimeOutput] = Field(default_factory=list)


class Reproduction(BaseModel):
    available: bool = False
    commands: list[str] = Field(default_factory=list)
    expected: str = ""
    actual: str = ""


class SuggestedFix(BaseModel):
    strategy: str = ""
    risk: str = "low"
    patch_diff: str | None = None  # 候选修复 diff（需验证后才发布）


class IssueVerification(BaseModel):
    status: VerificationStatus = VerificationStatus.NOT_RUN
    commands: list[str] = Field(default_factory=list)
    output_summary: str = ""


class IssuePublication(BaseModel):
    status: PublicationStatus = PublicationStatus.PENDING
    github_comment_id: int | None = None


class Issue(BaseModel):
    """单条审查发现，附带完整证据链。"""

    id: str
    review_id: str
    type: IssueType
    severity: Severity
    confidence: float = 0.0  # 0.0 ~ 1.0
    title: str
    file: str = ""
    line_start: int | None = None
    line_end: int | None = None
    introduced_by_pr: bool = True
    description: str = ""

    evidence: IssueEvidence = Field(default_factory=IssueEvidence)
    reproduction: Reproduction = Field(default_factory=Reproduction)
    suggested_fix: SuggestedFix | None = None
    verification: IssueVerification = Field(default_factory=IssueVerification)
    publication: IssuePublication = Field(default_factory=IssuePublication)

    producer: str = ""  # 产生该 issue 的 agent 名称

    def meets_patch_criteria(self, min_confidence: float = 0.8) -> bool:
        """是否满足自动 patch 触发条件（SPEC 5.5）。"""
        if self.severity not in (Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL):
            return False
        if self.confidence < min_confidence:
            return False
        return self.suggested_fix is not None and bool(self.suggested_fix.strategy)
