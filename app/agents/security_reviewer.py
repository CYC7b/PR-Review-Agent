"""Security Reviewer —— 安全审查 Agent（SPEC 7.3 / 5.3）。

审查安全敏感代码，结合 SAST、规则库和上下文分析安全问题。
安全发现区分：已验证漏洞、高置信度静态发现、低置信度风险提示。
高危问题发布时避免暴露可直接滥用的完整攻击步骤。
"""

from __future__ import annotations

import uuid
from typing import Any

from app.agents.base import BaseAgent
from app.blackboard import get_blackboard
from app.llm import LLMError, LLMResponse, get_llm_client
from app.llm.prompts import SECURITY_REVIEWER_SYSTEM
from app.models import (
    BlackboardMessage,
    Issue,
    IssueEvidence,
    IssueType,
    Severity,
    SuggestedFix,
    ToolOutput,
)
from app.tools.analyzer_tool import get_analyzer


class SecurityReviewer(BaseAgent):
    name = "SecurityReviewer"

    def __init__(self, review_id: str, head_sha: str, **kwargs: Any) -> None:
        super().__init__(review_id, head_sha, **kwargs)

    def run(
        self,
        changed_files: list[dict[str, Any]],
        file_contents: dict[str, str],
        diff: str = "",
        sast_findings: list[dict[str, Any]] | None = None,
    ) -> list[Issue]:
        self.check_cancelled()
        issues: list[Issue] = []

        # 1. 启发式 SAST 扫描（analyzer.security_scan）
        analyzer = get_analyzer()
        code_files = [f["path"] for f in changed_files if isinstance(f, dict)
                      and any(f["path"].endswith(ext) for ext in (".py", ".js", ".ts", ".go"))]
        heuristic_findings = analyzer.security_scan(code_files, file_contents)

        # 2. 合并外部 SAST 结果（沙箱中运行的 semgrep/bandit）
        if sast_findings:
            heuristic_findings.extend(sast_findings)

        for f in heuristic_findings:
            issue = self._finding_to_issue(f, diff)
            if issue:
                issues.append(issue)

        # 3. LLM 深度审查（增强，非唯一依据）
        llm_issues = self._llm_review(changed_files, file_contents, diff, heuristic_findings)
        issues.extend(llm_issues)

        # 4. 去重（同文件同行同类型）
        issues = self._deduplicate(issues)

        # 5. 写入 Blackboard
        bb = get_blackboard()
        for issue in issues:
            topic = f"issues.security.{issue.severity.value}"
            bb.publish(BlackboardMessage(
                topic=topic, review_id=self.review_id, producer=self.name,
                payload={"issue_id": issue.id, "summary": issue.title},
            ))

        self.logger.info("security_review.done",
                         findings=len(issues), heuristic=len(heuristic_findings))
        return issues

    def _finding_to_issue(self, finding: dict[str, Any], diff: str) -> Issue | None:
        try:
            severity = Severity(finding.get("severity", "medium"))
        except ValueError:
            severity = Severity.MEDIUM
        file_path = finding.get("file", "")
        line = finding.get("line")
        # 从 diff 中提取相关 hunk
        diff_hunk = self._extract_diff_hunk(diff, file_path, line) if diff else ""
        # 区分来源：外部 SAST（semgrep/bandit，在沙箱中执行）置信度更高，
        # 启发式正则较低；不再对所有发现硬编码 0.7（见 M2/安全置信度）。
        source = finding.get("source", "heuristic")
        tool_name = finding.get("tool", "heuristic-sast" if source == "heuristic" else source)
        if source == "sast":
            confidence = float(finding.get("confidence", 0.85))
        else:
            confidence = float(finding.get("confidence", 0.6))
        # introduced_by_pr 依据该位置是否出现在本 PR 的 diff 中判断，
        # 无法定位到 diff hunk 时不武断断言由 PR 引入。
        introduced = bool(diff_hunk)
        return Issue(
            id=f"issue-{uuid.uuid4().hex[:12]}",
            review_id=self.review_id,
            type=IssueType.SECURITY,
            severity=severity,
            confidence=confidence,
            title=finding.get("message", "安全风险"),
            file=file_path,
            line_start=line,
            line_end=line,
            introduced_by_pr=introduced,
            description=finding.get("message", ""),
            producer=self.name,
            evidence=IssueEvidence(
                diff_hunk=diff_hunk,
                tool_outputs=[ToolOutput(tool=tool_name, summary=finding.get("message", ""))],
            ),
            suggested_fix=SuggestedFix(strategy="请参考安全最佳实践", risk="medium"),
        )

    def _llm_review(
        self,
        changed_files: list[dict[str, Any]],
        file_contents: dict[str, str],
        diff: str,
        heuristic_findings: list[dict[str, Any]],
    ) -> list[Issue]:
        try:
            llm = get_llm_client()
            code_context = self._build_code_context(changed_files, file_contents)
            heuristic_summary = "\n".join(
                f"- {f.get('rule','?')}: {f.get('message','')} @ {f.get('file','')}:{f.get('line','?')}"
                for f in heuristic_findings
            )
            user_msg = (
                f"以下是 PR 变更的代码。请进行安全审查。\n\n"
                f"## Diff 摘要\n{diff[:3000]}\n\n"
                f"## 变更文件内容\n{code_context[:6000]}\n\n"
                f"## 启发式扫描结果\n{heuristic_summary or '无'}\n"
            )
            resp: LLMResponse = llm.chat(SECURITY_REVIEWER_SYSTEM, user_msg, json_mode=True, temperature=0.1)
            data = resp.parse_json()
            return self._parse_llm_issues(data.get("issues", []), diff)
        except LLMError as exc:
            self.logger.warning("security_review.llm_failed", error=str(exc))
            return []
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("security_review.llm_error", error=str(exc))
            return []

    def _parse_llm_issues(self, raw_issues: list[dict[str, Any]], diff: str) -> list[Issue]:
        result = []
        for raw in raw_issues:
            try:
                severity = Severity(raw.get("severity", "medium"))
            except ValueError:
                severity = Severity.MEDIUM
            conf = float(raw.get("confidence", 0.5))
            file_path = raw.get("file", "")
            line_start = raw.get("line_start")
            line_end = raw.get("line_end")
            diff_hunk = self._extract_diff_hunk(diff, file_path, line_start) if diff else ""
            issue = Issue(
                id=f"issue-{uuid.uuid4().hex[:12]}",
                review_id=self.review_id,
                type=IssueType.SECURITY,
                severity=severity,
                confidence=conf,
                title=raw.get("title", "安全发现"),
                file=file_path,
                line_start=line_start,
                line_end=line_end,
                introduced_by_pr=raw.get("introduced_by_pr", True),
                description=raw.get("description", ""),
                producer=self.name,
                evidence=IssueEvidence(
                    diff_hunk=diff_hunk or raw.get("evidence", {}).get("diff_hunk", ""),
                    related_code=raw.get("evidence", {}).get("related_code", ""),
                    tool_outputs=[ToolOutput(tool="llm-security-review",
                                             summary=raw.get("title", ""))],
                ),
                suggested_fix=SuggestedFix(
                    strategy=raw.get("suggested_fix", {}).get("strategy", ""),
                    risk=raw.get("suggested_fix", {}).get("risk", "medium"),
                ),
            )
            result.append(issue)
        return result

    @staticmethod
    def _build_code_context(changed_files: list[dict[str, Any]], file_contents: dict[str, str]) -> str:
        parts = []
        for f in changed_files[:20]:
            path = f.get("path", "")
            content = file_contents.get(path, "")
            if content:
                # 截断过长文件
                snippet = content[:2000]
                parts.append(f"### {path}\n```\n{snippet}\n```")
        return "\n\n".join(parts)

    @staticmethod
    def _extract_diff_hunk(diff: str, file_path: str, line: int | None) -> str:
        """从 unified diff 中提取与目标行相关的 hunk。"""
        if not diff or not file_path:
            return ""
        lines = diff.splitlines()
        in_file = False
        hunk_lines: list[str] = []
        for ln in lines:
            if ln.startswith("diff --git") or ln.startswith("---") or ln.startswith("+++"):
                if file_path.split("/")[-1] in ln:
                    in_file = True
                else:
                    if in_file and hunk_lines:
                        break
                    in_file = False
                continue
            if in_file and ln.startswith("@@"):
                if hunk_lines:
                    return "\n".join(hunk_lines)
                hunk_lines = [ln]
                continue
            if in_file:
                hunk_lines.append(ln)
        return "\n".join(hunk_lines[:30])

    @staticmethod
    def _deduplicate(issues: list[Issue]) -> list[Issue]:
        seen: set[tuple[str, str, int | None]] = set()
        result = []
        for issue in issues:
            key = (issue.file, issue.type.value, issue.line_start)
            if key in seen:
                continue
            seen.add(key)
            result.append(issue)
        return result
