"""Issue Aggregator 测试（SPEC 5.5）。"""

from __future__ import annotations

from app.agents.aggregator import IssueAggregator
from app.models import (
    Issue,
    IssueEvidence,
    IssueType,
    RuntimeOutput,
    Severity,
    SuggestedFix,
    ToolOutput,
    VerificationStatus,
)


def _make_issue(
    file="src/auth.py", line=42, itype=IssueType.SECURITY, sev=Severity.HIGH,
    conf=0.9, title="SQL injection", producer="SecurityReviewer",
    has_runtime=False, has_tool=False,
) -> Issue:
    evidence = IssueEvidence(diff_hunk="-old\n+new")
    if has_tool:
        evidence.tool_outputs = [ToolOutput(tool="semgrep", summary="detected")]
    if has_runtime:
        evidence.runtime_outputs = [RuntimeOutput(command="pytest", exit_code=1, summary="fail")]
    return Issue(
        id=f"issue-{file}-{line}", review_id="r1", type=itype, severity=sev,
        confidence=conf, title=title, file=file, line_start=line, line_end=line,
        producer=producer, evidence=evidence,
        suggested_fix=SuggestedFix(strategy="parameterized query", risk="low"),
    )


class TestAggregator:
    def test_deduplicate_same_location(self):
        agg = IssueAggregator(review_id="r1", head_sha="abc")
        issues = [
            _make_issue(line=42, conf=0.9, title="A"),
            _make_issue(line=43, conf=0.7, title="B"),  # 同一 ±3 行桶
        ]
        result = agg.run(issues)
        assert len(result["issues"]) == 1
        # 保留置信度更高的
        assert result["issues"][0].confidence == 0.9

    def test_deduplicate_different_locations(self):
        agg = IssueAggregator(review_id="r1", head_sha="abc")
        issues = [
            _make_issue(line=42),
            _make_issue(line=100),
        ]
        result = agg.run(issues)
        assert len(result["issues"]) == 2

    def test_suppress_low_confidence_no_evidence(self):
        agg = IssueAggregator(review_id="r1", head_sha="abc")
        issue = _make_issue(conf=0.2, title="low")
        issue.evidence.diff_hunk = ""
        issue.evidence.tool_outputs = []
        issue.evidence.runtime_outputs = []
        result = agg.run([issue])
        assert len(result["issues"]) == 0
        assert len(result["suppressed"]) == 1

    def test_keep_low_confidence_with_evidence(self):
        agg = IssueAggregator(review_id="r1", head_sha="abc")
        issue = _make_issue(conf=0.3, title="low", has_tool=True)
        result = agg.run([issue])
        assert len(result["issues"]) == 1

    def test_failing_runtime_marked_failed_not_passed(self):
        # 失败的 runtime 输出（exit_code=1）不得被误标为 PASSED（修复 M3）。
        agg = IssueAggregator(review_id="r1", head_sha="abc")
        issue = _make_issue(has_runtime=True)  # runtime exit_code=1
        result = agg.run([issue])
        assert result["issues"][0].verification.status == VerificationStatus.FAILED

    def test_passing_runtime_marked_passed(self):
        # 仅当存在 exit_code==0 的核验输出时才标 PASSED。
        agg = IssueAggregator(review_id="r1", head_sha="abc")
        issue = _make_issue()
        issue.evidence.runtime_outputs = [
            RuntimeOutput(command="pytest", exit_code=0, summary="ok")
        ]
        result = agg.run([issue])
        assert result["issues"][0].verification.status == VerificationStatus.PASSED

    def test_sorting_by_severity(self):
        agg = IssueAggregator(review_id="r1", head_sha="abc")
        issues = [
            _make_issue(sev=Severity.LOW, line=10, title="low"),
            _make_issue(sev=Severity.CRITICAL, line=20, title="crit"),
            _make_issue(sev=Severity.HIGH, line=30, title="high"),
        ]
        result = agg.run(issues)
        severities = [i.severity for i in result["issues"]]
        assert severities[0] == Severity.CRITICAL
        assert severities[1] == Severity.HIGH
        assert severities[2] == Severity.LOW

    def test_patch_candidates(self):
        agg = IssueAggregator(review_id="r1", head_sha="abc")
        from app.config import get_config
        cfg = get_config()
        cfg.review.enable_auto_patch = True
        issue = _make_issue(sev=Severity.HIGH, conf=0.95)
        result = agg.run([issue])
        assert len(result["patch_candidates"]) == 1
        assert result["should_generate_patch"] is True

    def test_no_patch_when_disabled(self):
        agg = IssueAggregator(review_id="r1", head_sha="abc")
        from app.config import get_config
        cfg = get_config()
        cfg.review.enable_auto_patch = False
        issue = _make_issue(sev=Severity.HIGH, conf=0.95)
        result = agg.run([issue])
        assert result["should_generate_patch"] is False
