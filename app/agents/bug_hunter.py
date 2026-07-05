"""Bug Hunter —— 缺陷审查 Agent（SPEC 7.4 / 5.3）。

检查逻辑正确性、边界条件、错误处理、并发与状态一致性。
必要时生成最小复现脚本（简短、可读、可删除）。
"""

from __future__ import annotations

import uuid
from typing import Any

from app.agents.base import BaseAgent
from app.blackboard import get_blackboard
from app.llm import LLMError, LLMResponse, get_llm_client
from app.llm.prompts import BUG_HUNTER_SYSTEM
from app.models import (
    BlackboardMessage,
    Issue,
    IssueEvidence,
    IssueType,
    Reproduction,
    Severity,
    SuggestedFix,
    ToolOutput,
)
from app.tools.analyzer_tool import get_analyzer


class BugHunter(BaseAgent):
    name = "BugHunter"

    def __init__(self, review_id: str, head_sha: str, **kwargs: Any) -> None:
        super().__init__(review_id, head_sha, **kwargs)

    def run(
        self,
        changed_files: list[dict[str, Any]],
        file_contents: dict[str, str],
        diff: str = "",
    ) -> list[Issue]:
        self.check_cancelled()
        issues: list[Issue] = []

        # 1. 启发式风格/可维护性扫描（基础缺陷信号）
        analyzer = get_analyzer()
        code_files = [f["path"] for f in changed_files if isinstance(f, dict)
                      and any(f["path"].endswith(ext) for ext in (".py", ".js", ".ts", ".go"))]
        for path in code_files:
            lang = "python" if path.endswith(".py") else "javascript"
            lint_findings = analyzer.lint([path], lang, file_contents)
            for f in lint_findings:
                if f["rule"] == "bare-except":  # 裸 except 是潜在 bug
                    issues.append(self._lint_to_issue(f, diff))

        # 2. LLM 深度缺陷审查
        llm_issues = self._llm_review(changed_files, file_contents, diff)
        issues.extend(llm_issues)

        # 3. 去重
        issues = self._deduplicate(issues)

        # 4. 写入 Blackboard
        bb = get_blackboard()
        for issue in issues:
            topic = f"issues.bug.{issue.severity.value}"
            bb.publish(BlackboardMessage(
                topic=topic, review_id=self.review_id, producer=self.name,
                payload={"issue_id": issue.id, "summary": issue.title},
            ))

        self.logger.info("bug_hunter.done", findings=len(issues))
        return issues

    def _lint_to_issue(self, finding: dict[str, Any], diff: str) -> Issue:
        return Issue(
            id=f"issue-{uuid.uuid4().hex[:12]}",
            review_id=self.review_id,
            type=IssueType.BUG,
            severity=Severity.LOW,
            confidence=0.6,
            title=finding.get("message", "潜在缺陷"),
            file=finding.get("file", ""),
            line_start=finding.get("line"),
            line_end=finding.get("line"),
            introduced_by_pr=True,
            description=finding.get("message", ""),
            producer=self.name,
            evidence=IssueEvidence(
                tool_outputs=[ToolOutput(tool="heuristic-lint", summary=finding.get("message", ""))],
            ),
            suggested_fix=SuggestedFix(strategy="使用具体异常类型替代裸 except", risk="low"),
        )

    def _llm_review(
        self,
        changed_files: list[dict[str, Any]],
        file_contents: dict[str, str],
        diff: str,
    ) -> list[Issue]:
        try:
            llm = get_llm_client()
            code_context = self._build_code_context(changed_files, file_contents)
            user_msg = (
                f"以下是 PR 变更的代码。请检查逻辑正确性、边界条件、错误处理、并发风险。\n\n"
                f"## Diff\n{diff[:3000]}\n\n"
                f"## 变更文件内容\n{code_context[:6000]}\n"
            )
            resp: LLMResponse = llm.chat(BUG_HUNTER_SYSTEM, user_msg, json_mode=True, temperature=0.1)
            data = resp.parse_json()
            return self._parse_llm_issues(data.get("issues", []), diff)
        except LLMError as exc:
            self.logger.warning("bug_hunter.llm_failed", error=str(exc))
            return []
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("bug_hunter.llm_error", error=str(exc))
            return []

    def _parse_llm_issues(self, raw_issues: list[dict[str, Any]], diff: str) -> list[Issue]:
        result = []
        for raw in raw_issues:
            try:
                severity = Severity(raw.get("severity", "medium"))
            except ValueError:
                severity = Severity.MEDIUM
            conf = float(raw.get("confidence", 0.5))
            repro_raw = raw.get("reproduction", {})
            issue = Issue(
                id=f"issue-{uuid.uuid4().hex[:12]}",
                review_id=self.review_id,
                type=IssueType.BUG,
                severity=severity,
                confidence=conf,
                title=raw.get("title", "缺陷发现"),
                file=raw.get("file", ""),
                line_start=raw.get("line_start"),
                line_end=raw.get("line_end"),
                introduced_by_pr=raw.get("introduced_by_pr", True),
                description=raw.get("description", ""),
                producer=self.name,
                evidence=IssueEvidence(
                    diff_hunk=raw.get("evidence", {}).get("diff_hunk", ""),
                    related_code=raw.get("evidence", {}).get("related_code", ""),
                    tool_outputs=[ToolOutput(tool="llm-bug-review", summary=raw.get("title", ""))],
                ),
                reproduction=Reproduction(
                    # available 表示"已实际执行并复现"。BugHunter 不执行复现脚本，
                    # 因此这里恒为 False；commands 仅作为 LLM 建议的候选复现命令，
                    # 真正的执行验证由沙箱/TestExecutor 完成（L2）。
                    available=False,
                    commands=repro_raw.get("commands", []),
                    expected=repro_raw.get("expected", ""),
                    actual=repro_raw.get("actual", ""),
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
                snippet = content[:2000]
                parts.append(f"### {path}\n```\n{snippet}\n```")
        return "\n\n".join(parts)

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
