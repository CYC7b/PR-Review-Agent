"""数据模型测试（SPEC 第 6 节）。"""

from __future__ import annotations

from app.models import (
    Issue,
    IssueEvidence,
    IssueType,
    IssueVerification,
    Patch,
    PatchPublishMode,
    ReviewPlan,
    ReviewState,
    ReviewTask,
    Severity,
    SuggestedFix,
    ToolOutput,
    VerificationStatus,
)


class TestReviewTask:
    def test_task_key(self):
        task = ReviewTask(
            review_id="repo-1-pr-2-sha-abc",
            repository_id=1,
            repository_full_name="org/repo",
            pr_number=2,
            base_sha="base123",
            head_sha="abc1234567",
        )
        assert task.task_key == "1:2:abc1234567"

    def test_transition_normal(self):
        task = ReviewTask(
            review_id="r1", repository_id=1, repository_full_name="o/r",
            pr_number=1, base_sha="b", head_sha="h",
        )
        task.transition(ReviewState.VALIDATING_EVENT)
        assert task.status == ReviewState.VALIDATING_EVENT
        assert task.started_at is not None

    def test_transition_to_terminal_sets_completed_at(self):
        task = ReviewTask(
            review_id="r1", repository_id=1, repository_full_name="o/r",
            pr_number=1, base_sha="b", head_sha="h",
            status=ReviewState.REVIEW_PUBLISHING,
        )
        task.transition(ReviewState.COMPLETED)
        assert task.completed_at is not None

    def test_transition_from_terminal_raises(self):
        task = ReviewTask(
            review_id="r1", repository_id=1, repository_full_name="o/r",
            pr_number=1, base_sha="b", head_sha="h",
            status=ReviewState.COMPLETED,
        )
        import pytest

        with pytest.raises(ValueError):
            task.transition(ReviewState.PREPROCESSING)


class TestIssue:
    def test_meets_patch_criteria_high_severity(self):
        issue = Issue(
            id="i1", review_id="r1", type=IssueType.SECURITY, severity=Severity.HIGH,
            confidence=0.9, title="SQL injection",
            suggested_fix=SuggestedFix(strategy="parameterized query"),
        )
        assert issue.meets_patch_criteria(0.8) is True

    def test_meets_patch_criteria_low_confidence(self):
        issue = Issue(
            id="i1", review_id="r1", type=IssueType.SECURITY, severity=Severity.HIGH,
            confidence=0.5, title="SQL injection",
            suggested_fix=SuggestedFix(strategy="fix"),
        )
        assert issue.meets_patch_criteria(0.8) is False

    def test_meets_patch_criteria_low_severity(self):
        issue = Issue(
            id="i1", review_id="r1", type=IssueType.STYLE, severity=Severity.LOW,
            confidence=0.95, title="style",
            suggested_fix=SuggestedFix(strategy="fix"),
        )
        assert issue.meets_patch_criteria(0.8) is False

    def test_meets_patch_criteria_no_suggested_fix(self):
        issue = Issue(
            id="i1", review_id="r1", type=IssueType.BUG, severity=Severity.MEDIUM,
            confidence=0.9, title="bug",
        )
        assert issue.meets_patch_criteria(0.8) is False

    def test_issue_serialization(self):
        issue = Issue(
            id="i1", review_id="r1", type=IssueType.SECURITY, severity=Severity.HIGH,
            confidence=0.92, title="SQL injection", file="src/auth.py",
            line_start=42, line_end=48,
            evidence=IssueEvidence(
                diff_hunk="-old\n+new",
                tool_outputs=[ToolOutput(tool="semgrep", summary="detected")],
            ),
            verification=IssueVerification(status=VerificationStatus.PASSED),
        )
        data = issue.model_dump()
        restored = Issue(**data)
        assert restored.severity == Severity.HIGH
        assert restored.confidence == 0.92
        assert restored.verification.status == VerificationStatus.PASSED


class TestPatch:
    def test_is_publishable_passed(self):
        from app.models import PatchValidation
        patch = Patch(
            patch_id="p1", review_id="r1", issue_ids=["i1"],
            validation=PatchValidation(status=VerificationStatus.PASSED),
            publish_mode=PatchPublishMode.GITHUB_SUGGESTION,
        )
        assert patch.is_publishable_as_suggestion() is True

    def test_is_publishable_failed(self):
        from app.models import PatchValidation
        patch = Patch(
            patch_id="p1", review_id="r1",
            validation=PatchValidation(status=VerificationStatus.FAILED),
        )
        assert patch.is_publishable_as_suggestion() is False


class TestReviewPlan:
    def test_plan_serialization(self):
        from app.models import PlannedFile, PlannedTask
        plan = ReviewPlan(
            review_id="r1", head_sha="abc",
            language_profile=["python"],
            changed_files=[PlannedFile(path="src/auth.py", risk_level="high", reasons=["auth"])],
            tasks=[PlannedTask(id="t1", agent="SecurityReviewer", priority="high")],
            sandbox_required=True,
        )
        data = plan.model_dump()
        assert data["language_profile"] == ["python"]
        assert data["sandbox_required"] is True
