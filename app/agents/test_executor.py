"""Test Executor —— 测试执行 Agent（SPEC 7.5 / 5.4）。

在沙箱中运行测试套件、lint、type check、安全扫描。
区分：环境问题、依赖安装失败、超时；对代码失败依据失败输出是否引用变更文件
进行 PR 归因（未做 base 分支基线对比时不武断断言 PR 引入，见 H5）。
"""

from __future__ import annotations

import uuid
from typing import Any

from app.agents.base import BaseAgent
from app.blackboard import get_blackboard
from app.config import get_config
from app.logging_setup import get_logger
from app.models import (
    BlackboardMessage,
    Issue,
    IssueEvidence,
    IssueType,
    RuntimeOutput,
    Severity,
    VerificationStatus,
)
from app.tools.analyzer_tool import get_analyzer


class TestExecutor(BaseAgent):
    name = "TestExecutor"

    def __init__(self, review_id: str, head_sha: str, sandbox_id: str | None = None,
                 repository_full_name: str = "", base_sha: str = "", **kwargs: Any) -> None:
        super().__init__(review_id, head_sha, **kwargs)
        self.sandbox_id = sandbox_id
        self.repository_full_name = repository_full_name
        self.base_sha = base_sha
        self.logger = get_logger(self.name)

    def run(
        self,
        changed_files: list[dict[str, Any]],
        repo_files: list[str] | None = None,
        custom_commands: list[str] | None = None,
    ) -> dict[str, Any]:
        """执行测试/lint/SAST，返回结果与产生的 issue。"""
        self.check_cancelled()
        cfg = get_config()

        if not self.sandbox_id:
            self.logger.warning("test_executor.no_sandbox")
            return {"status": "skipped", "reason": "无沙箱环境", "issues": [], "commands": []}

        analyzer = get_analyzer()
        # 1. 检测测试命令
        candidate_cmds = custom_commands or []
        if not candidate_cmds and repo_files:
            detected = analyzer.detect_test_commands(repo_files)
            candidate_cmds = [d["cmd"] for d in detected if d["cmd"] != "__CI_CONFIG__"]

        # 2. 执行命令
        results: list[dict[str, Any]] = []
        issues: list[Issue] = []
        for cmd in candidate_cmds:
            self.check_cancelled()
            r = self._run_command(cmd, timeout=cfg.sandbox.default_timeout_seconds)
            results.append({"command": cmd, **r})
            if r["exit_code"] != 0 and not r["timed_out"]:
                issue = self._failure_to_issue(cmd, r, changed_files)
                if issue:
                    issues.append(issue)
            elif r["timed_out"]:
                issues.append(self._timeout_issue(cmd))

        # 3. 写入 Blackboard
        bb = get_blackboard()
        bb.publish(BlackboardMessage(
            topic="test_results", review_id=self.review_id, producer=self.name,
            payload={"results": results, "all_passed": all(r["exit_code"] == 0 for r in results)},
        ))
        for issue in issues:
            bb.publish(BlackboardMessage(
                topic=f"issues.test_failure.{issue.severity.value}",
                review_id=self.review_id, producer=self.name,
                payload={"issue_id": issue.id, "summary": issue.title},
            ))

        all_passed = all(r["exit_code"] == 0 for r in results) if results else True
        self.logger.info("test_executor.done", commands=len(results), passed=all_passed, issues=len(issues))
        return {
            "status": "passed" if all_passed else "failed",
            "results": results,
            "issues": issues,
            "commands": candidate_cmds,
        }

    def _run_command(self, command: str, timeout: int = 300) -> dict[str, Any]:
        """通过沙箱执行命令。"""
        try:
            result = self.tool("sandbox.exec", sandbox_id=self.sandbox_id,
                               command=command, timeout=timeout)
            return result.to_dict() if hasattr(result, "to_dict") else result
        except Exception as exc:  # noqa: BLE001
            self.logger.error("test_executor.command_failed", command=command, error=str(exc))
            return {"stdout": "", "stderr": str(exc), "exit_code": -1, "timed_out": False}

    def _failure_to_issue(
        self, command: str, result: dict[str, Any], changed_files: list[dict[str, Any]]
    ) -> Issue | None:
        """将测试/lint 失败转为 issue，判断是否 PR 相关。"""
        stderr = result.get("stderr", "")
        stdout = result.get("stdout", "")

        # 判断失败类别
        failure_category = self._classify_failure(stderr + "\n" + stdout, command)

        # 环境问题/依赖安装失败不作为代码 issue
        if failure_category == "environment":
            self.logger.info("test_executor.env_failure", command=command)
            return None
        if failure_category == "dependency":
            self.logger.info("test_executor.dependency_failure", command=command)
            return None

        severity = Severity.HIGH if "test" in command else Severity.MEDIUM
        # 尝试定位到变更文件
        target_file = self._locate_failing_file(stderr + stdout, changed_files)

        # H5：不再把所有失败武断标记为 PR 引入。仅当失败输出引用了本次变更文件时，
        # 才认为与 PR 相关（高置信度）；否则无法在缺少 base 基线对比的情况下归因，
        # 明确标注 introduced_by_pr=False 并降低置信度，不伪造 0.85 的 pr_related。
        attributed_to_pr = target_file is not None
        description = self._truncate(stderr or stdout, 1000)
        if attributed_to_pr:
            confidence = 0.8
        else:
            confidence = 0.4
            description = (
                "注意：未运行 base 分支基线对比，无法确认该失败是否由本 PR 引入"
                "（可能是仓库预存在的失败）。\n\n" + description
            )

        return Issue(
            id=f"issue-{uuid.uuid4().hex[:12]}",
            review_id=self.review_id,
            type=IssueType.TEST_FAILURE,
            severity=severity,
            confidence=confidence,
            title=f"命令失败: {command}",
            file=target_file or "",
            introduced_by_pr=attributed_to_pr,
            description=description,
            producer=self.name,
            evidence=IssueEvidence(
                runtime_outputs=[RuntimeOutput(
                    command=command,
                    exit_code=result.get("exit_code"),
                    summary=self._truncate(stderr or stdout, 500),
                    stdout=self._truncate(stdout, 2000),
                    stderr=self._truncate(stderr, 2000),
                )],
            ),
        )

    def _timeout_issue(self, command: str) -> Issue:
        from app.models import IssueVerification
        return Issue(
            id=f"issue-{uuid.uuid4().hex[:12]}",
            review_id=self.review_id,
            type=IssueType.TEST_FAILURE,
            severity=Severity.MEDIUM,
            confidence=0.9,
            title=f"命令超时: {command}",
            file="",
            introduced_by_pr=False,
            description=f"执行超时: {command}",
            producer=self.name,
            evidence=IssueEvidence(
                runtime_outputs=[RuntimeOutput(command=command, exit_code=None, summary="TIMEOUT")],
            ),
            verification=IssueVerification(status=VerificationStatus.TIMEOUT, commands=[command]),
        )

    @staticmethod
    def _classify_failure(output: str, command: str) -> str:
        """区分失败类别：environment / dependency / code_failure。

        仅识别可确定的环境与依赖失败；其余归为通用 code_failure，
        由 _failure_to_issue 依据"失败是否引用变更文件"进一步归因，
        不再武断断言 PR 引入（H5）。
        """
        lower = output.lower()
        if any(kw in lower for kw in ("modulenotfounderror", "importerror", "no module named",
                                       "cannot find module", "package not found")):
            return "dependency"
        if any(kw in lower for kw in ("permission denied", "no such file or directory",
                                       "connection refused", "address already in use",
                                       "port is already allocated")):
            return "environment"
        return "code_failure"

    @staticmethod
    def _locate_failing_file(output: str, changed_files: list[dict[str, Any]]) -> str | None:
        """从测试输出中定位失败的变更文件。"""
        changed_paths = [f["path"] if isinstance(f, dict) else str(f) for f in changed_files]
        for path in changed_paths:
            if path in output:
                return path
            # 也匹配文件名
            basename = path.rsplit("/", 1)[-1]
            if basename in output and basename != path:
                return path
        return None

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        return text if len(text) <= limit else text[:limit] + "...(truncated)"
