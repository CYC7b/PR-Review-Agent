"""Review Publisher —— 审查结果发布（SPEC 7.7 / 5.6 / 12.2）。

根据问题类型选择输出方式：
  - 小范围确定性修复 → GitHub suggestion
  - 有证据但不适合自动修复 → 行级 review comment
  - 全局设计问题 → Summary comment
  - 测试失败 → Check summary 或 PR comment
  - 安全高危问题 → 精简披露，避免可滥用细节
  - 低置信度问题 → 可选 summary，不阻塞 PR

发布前检查：head SHA 一致、issue 未发布、评论不重复、suggestion 可应用。
优先使用 pending review 批量发布。
"""

from __future__ import annotations

from typing import Any

from app.agents.base import BaseAgent
from app.config import get_config
from app.logging_setup import get_logger
from app.models import (
    Issue,
    Patch,
    PatchPublishMode,
    PublicationStatus,
    ReviewPlan,
    ReviewTask,
    Severity,
    VerificationStatus,
)

# 发布格式模板
_SEVERITY_LABEL = {
    Severity.CRITICAL: "Critical",
    Severity.HIGH: "High",
    Severity.MEDIUM: "Medium",
    Severity.LOW: "Low",
    Severity.INFO: "Info",
}


class HeadShaMismatch(Exception):
    """PR 在发布前更新，head SHA 不匹配（应进入 SUPERSEDED）。"""


class ReviewPublisher(BaseAgent):
    name = "ReviewPublisher"

    def __init__(self, review_id: str, head_sha: str, repository_full_name: str,
                 pr_number: int, **kwargs: Any) -> None:
        super().__init__(review_id, head_sha, **kwargs)
        self.repository_full_name = repository_full_name
        self.pr_number = pr_number
        self.logger = get_logger(self.name)

    def run(
        self,
        task: ReviewTask,
        issues: list[Issue],
        patches: list[Patch],
        plan: ReviewPlan | None = None,
        test_results: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.check_cancelled()
        cfg = get_config().github

        # 1. 校验 head SHA 仍匹配（SPEC 12.2）
        current_sha = self._fetch_current_head_sha()
        if cfg.bind_to_head_sha and current_sha and current_sha != self.head_sha:
            raise HeadShaMismatch(
                f"head SHA 已变更: {self.head_sha[:8]} → {current_sha[:8]}"
            )

        # 2. 准备 review comments（行级）与 summary
        review_comments: list[dict[str, Any]] = []
        summary_issues: list[Issue] = []

        patch_by_issue = {pid: p for p in patches for pid in p.issue_ids}

        for issue in issues:
            if issue.publication.status == PublicationStatus.PUBLISHED:
                continue  # 已发布，去重
            if issue.publication.status == PublicationStatus.SUPPRESSED:
                summary_issues.append(issue)
                continue

            comment_body = self._format_issue_comment(issue, patch_by_issue.get(issue.id))

            # 全局问题或无文件定位 → summary
            if not issue.file or issue.severity == Severity.INFO:
                summary_issues.append(issue)
                continue

            # 有行号 → 行级 review comment
            if issue.line_start:
                review_comments.append({
                    "path": issue.file,
                    "line": issue.line_end or issue.line_start,
                    "side": "RIGHT",
                    "body": comment_body,
                })
                issue.publication.status = PublicationStatus.PUBLISHED
            else:
                summary_issues.append(issue)

        # 3. 构建 summary
        summary_body = self._build_summary(task, issues, patches, plan, test_results)

        # 4. 批量发布（优先 pending review）
        published: dict[str, Any] = {}
        if review_comments:
            try:
                result = self.tool(
                    "github.create_review",
                    repo=self.repository_full_name,
                    pr_number=self.pr_number,
                    head_sha=self.head_sha,
                    comments=review_comments,
                    body=summary_body,
                    event="COMMENT",
                )
                published["review"] = result
            except Exception as exc:  # noqa: BLE001
                self.logger.error("publisher.review_failed", error=str(exc))
                # 降级：单独发 comment
                self.tool("github.create_comment",
                          repo=self.repository_full_name,
                          pr_number=self.pr_number, body=summary_body)
        else:
            # 无行级评论 → 发 summary comment
            try:
                self.tool("github.create_comment",
                          repo=self.repository_full_name,
                          pr_number=self.pr_number, body=summary_body)
            except Exception as exc:  # noqa: BLE001
                self.logger.error("publisher.summary_failed", error=str(exc))

        # 5. 发布 check run
        conclusion = self._determine_conclusion(issues, patches, test_results)
        try:
            self.tool("github.create_check_run",
                      repo=self.repository_full_name,
                      head_sha=self.head_sha,
                      conclusion=conclusion,
                      summary=self._check_summary(issues, patches, test_results))
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("publisher.check_run_failed", error=str(exc))

        self.logger.info("publisher.done",
                         comments=len(review_comments), conclusion=conclusion)
        return {"published": published, "conclusion": conclusion, "comment_count": len(review_comments)}

    def _fetch_current_head_sha(self) -> str | None:
        """获取 PR 当前 head SHA（用于检测是否被更新）。"""
        try:
            pr = self.tool("github.get_pr",
                           repo=self.repository_full_name, pr_number=self.pr_number)
            return pr.get("head_sha")
        except Exception:  # noqa: BLE001
            return None

    def _format_issue_comment(self, issue: Issue, patch: Patch | None) -> str:
        """格式化单条 issue 的评论内容（SPEC 19.1）。"""
        sev_label = _SEVERITY_LABEL.get(issue.severity, issue.severity.value)
        lines = [
            f"**Severity:** {sev_label}  ",
            f"**Confidence:** {issue.confidence:.2f}  ",
            f"**Type:** {issue.type.value}  ",
            "",
            issue.title,
            "",
        ]
        if issue.description:
            lines.extend([issue.description, ""])

        # 证据
        lines.append("**Evidence**")
        if issue.file:
            lines.append(f"- File: `{issue.file}`")
        if issue.line_start:
            lines.append(f"- Line: {issue.line_start}")
        if issue.evidence.diff_hunk:
            lines.append("- Diff hunk:")
            lines.append(f"```diff\n{issue.evidence.diff_hunk[:500]}\n```")
        for t in issue.evidence.tool_outputs:
            lines.append(f"- `{t.tool}`: {t.summary}")
        for r in issue.evidence.runtime_outputs:
            lines.append(f"- `{r.command}` (exit {r.exit_code}): {r.summary}")
        lines.append("")

        # 验证状态
        if issue.verification.status != VerificationStatus.NOT_RUN:
            lines.append(f"**Verification:** {issue.verification.status.value}")
            if issue.verification.commands:
                lines.append("```bash")
                lines.extend(issue.verification.commands)
                lines.append("```")
            lines.append("")

        # 安全高危精简披露（SPEC 17.3）
        if issue.type.value == "security" and issue.severity in (Severity.CRITICAL, Severity.HIGH):
            lines.append("> ⚠️ 安全高危问题：为避免暴露可滥用细节，完整复现步骤未在公开评论中展示。")
            lines.append("")

        # suggestion（验证通过的 patch）
        if patch and patch.publish_mode == PatchPublishMode.GITHUB_SUGGESTION and patch.diff:
            lines.append("**Suggested fix**")
            lines.append("```suggestion")
            # 从 diff 提取新增行作为 suggestion
            added = [line[1:] for line in patch.diff.splitlines() if line.startswith("+") and not line.startswith("+++")]
            if added:
                lines.extend(added)
            else:
                lines.append(f"Strategy: {patch.strategy}")
            lines.append("```")
            lines.append(f"_Patch 已通过沙箱验证 ({patch.validation.status.value})_")
        elif issue.suggested_fix and issue.suggested_fix.strategy:
            lines.append(f"**Suggested fix:** {issue.suggested_fix.strategy}")

        return "\n".join(lines)

    def _build_summary(
        self,
        task: ReviewTask,
        issues: list[Issue],
        patches: list[Patch],
        plan: ReviewPlan | None,
        test_results: dict[str, Any] | None,
    ) -> str:
        """构建 PR 审查 summary（SPEC 19.2）。"""
        lines = [
            "## PR Review Agent Summary",
            "",
            f"Reviewed commit: `{task.head_sha[:12]}`",
            f"Status: {task.status.value}",
            "",
            "### Findings",
            "",
            "| Severity | Type | File | Status |",
            "|---|---|---|---|",
        ]
        for issue in issues[:50]:
            status = issue.verification.status.value
            if issue.publication.status == PublicationStatus.SUPPRESSED:
                status = "suppressed"
            file_short = issue.file.rsplit("/", 1)[-1] if issue.file else "-"
            lines.append(
                f"| {_SEVERITY_LABEL.get(issue.severity, '?')} | {issue.type.value} | "
                f"`{file_short}` | {status} |"
            )
        if not issues:
            lines.append("| - | No issues found | - | - |")

        # 验证结果
        lines.extend(["", "### Validation"])
        report = test_results.get("report") if test_results else None
        if report:
            report_data = report.model_dump(mode="json") if hasattr(report, "model_dump") else report
            lines.append(f"- Overall: `{report_data.get('status', 'unknown')}`")
            for limitation in report_data.get("limitations", []):
                lines.append(f"- Limitation: {limitation}")
            for regression in report_data.get("confirmed_regressions", []):
                lines.append(f"- Confirmed regression: `{regression}`")
        if test_results and test_results.get("results"):
            for r in test_results["results"]:
                status = "passed" if r.get("exit_code") == 0 else "failed"
                lines.append(f"- `{r['command']}`: {status}")
        else:
            lines.append("- No tests executed")

        # Patch 状态
        lines.extend(["", "### Patch status"])
        verified = [p for p in patches if p.validation.status == VerificationStatus.PASSED]
        if verified:
            lines.append(f"{len(verified)} verified GitHub suggestion(s) generated.")
            lines.append("No unverified patch was published.")
        elif patches:
            lines.append("Patches generated but none passed validation. No suggestion published.")
        else:
            lines.append("No patches generated.")

        return "\n".join(lines)

    @staticmethod
    def _determine_conclusion(issues: list[Issue], patches: list[Patch],
                              test_results: dict[str, Any] | None = None) -> str:
        """决定 check run 结论。"""
        blocking = [i for i in issues if i.severity in (Severity.CRITICAL, Severity.HIGH)
                    and i.publication.status != PublicationStatus.SUPPRESSED
                    and (i.type.value != "test_failure" or i.introduced_by_pr)]
        if blocking:
            return "failure"
        report = test_results.get("report") if test_results else None
        report_status = report.status.value if hasattr(report, "status") else (report or {}).get("status") if report else ""
        if report_status in {"failed", "incomplete", "timeout", "flaky"}:
            return "neutral"
        if any(i.severity == Severity.MEDIUM for i in issues):
            return "neutral"
        return "success"

    @staticmethod
    def _check_summary(issues: list[Issue], patches: list[Patch],
                       test_results: dict[str, Any] | None = None) -> str:
        total = len(issues)
        verified = sum(1 for p in patches if p.validation.status == VerificationStatus.PASSED)
        report = test_results.get("report") if test_results else None
        status = report.status.value if hasattr(report, "status") else (report or {}).get("status", "not-run")
        return f"Reviewed {total} finding(s); tests={status}; {verified} verified suggestion(s)."
