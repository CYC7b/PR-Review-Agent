"""Patch Generator —— 修复生成与验证（SPEC 7.6 / 第 10 节）。

对高置信度、小范围、可验证问题生成最小修复 patch 并在沙箱中验证。
约束：最小改动、不修改无关文件、不引入新依赖、不修改公开接口、
不删除测试绕过失败、不降低安全校验、不吞掉异常通过测试。
"""

from __future__ import annotations

import uuid
from typing import Any

from app.agents.base import BaseAgent
from app.config import get_config
from app.llm import LLMError, LLMResponse, get_llm_client
from app.llm.prompts import PATCH_GENERATOR_SYSTEM
from app.models import (
    Issue,
    Patch,
    PatchPublishMode,
    PatchValidation,
    VerificationStatus,
)


class PatchGenerator(BaseAgent):
    name = "PatchGenerator"

    def __init__(self, review_id: str, head_sha: str, sandbox_id: str | None = None,
                 file_contents: dict[str, str] | None = None, **kwargs: Any) -> None:
        super().__init__(review_id, head_sha, **kwargs)
        self.sandbox_id = sandbox_id
        self.file_contents = file_contents or {}

    def run(self, issues: list[Issue], test_commands: list[str] | None = None) -> list[Patch]:
        self.check_cancelled()
        cfg = get_config().patch
        patches: list[Patch] = []

        for issue in issues:
            self.check_cancelled()
            if not issue.meets_patch_criteria(cfg.min_confidence_for_patch):
                continue
            patch = self._generate_and_validate(issue, test_commands, cfg.max_retry)
            if patch:
                patches.append(patch)

        self.logger.info("patch_generator.done",
                         candidates=len(issues), generated=len(patches))
        return patches

    def _generate_and_validate(
        self, issue: Issue, test_commands: list[str] | None, max_retry: int
    ) -> Patch | None:
        patch_id = f"patch-{uuid.uuid4().hex[:12]}"
        retry = 0
        last_diff = ""

        while retry <= max_retry:
            self.check_cancelled()
            # 1. 生成 patch
            diff = self._generate_diff(issue, retry)
            if not diff:
                self.logger.info("patch_generator.no_diff", issue=issue.id, retry=retry)
                break
            last_diff = diff

            # 2. 约束检查
            if not self._check_constraints(diff, issue):
                self.logger.warning("patch_generator.constraint_violation", issue=issue.id, retry=retry)
                retry += 1
                continue

            # 3. 应用 patch
            if not self.sandbox_id:
                # 无沙箱 → 无法验证，标记 skipped
                return Patch(
                    patch_id=patch_id,
                    issue_ids=[issue.id],
                    review_id=self.review_id,
                    diff=diff,
                    files_changed=[issue.file] if issue.file else [],
                    validation=PatchValidation(status=VerificationStatus.SKIPPED,
                                               summary="无沙箱环境，跳过验证"),
                    publish_mode=PatchPublishMode.SUPPRESSED,
                    retry_count=retry,
                )

            apply_result = self.tool("sandbox.apply_patch", sandbox_id=self.sandbox_id,
                                     patch_content=diff)
            if not apply_result.get("applied", False):
                self.logger.info("patch_generator.apply_failed", issue=issue.id,
                                 retry=retry, error=apply_result.get("error"))
                retry += 1
                continue

            # 4. 验证
            validation = self._validate(test_commands or [])

            # 5. 回滚 patch（验证后恢复原始状态，避免影响后续 patch）
            self.tool("sandbox.exec", sandbox_id=self.sandbox_id,
                      command="git checkout -- .", timeout=30)

            if validation.status == VerificationStatus.PASSED:
                return Patch(
                    patch_id=patch_id,
                    issue_ids=[issue.id],
                    review_id=self.review_id,
                    strategy=issue.suggested_fix.strategy if issue.suggested_fix else "minimal_fix",
                    diff=diff,
                    files_changed=[issue.file] if issue.file else [],
                    risk_level=issue.suggested_fix.risk if issue.suggested_fix else "low",
                    validation=validation,
                    publish_mode=PatchPublishMode.GITHUB_SUGGESTION,
                    retry_count=retry,
                )
            retry += 1

        # 重试耗尽 → 不发布 suggestion（SPEC 10.4）
        return Patch(
            patch_id=patch_id,
            issue_ids=[issue.id],
            review_id=self.review_id,
            diff=last_diff,
            files_changed=[issue.file] if issue.file else [],
            validation=PatchValidation(status=VerificationStatus.FAILED,
                                       summary="patch 验证失败，重试耗尽"),
            publish_mode=PatchPublishMode.SUPPRESSED,
            retry_count=retry,
        )

    def _generate_diff(self, issue: Issue, retry: int) -> str:
        """通过 LLM 生成修复 diff。"""
        try:
            llm = get_llm_client()
            file_content = self.file_contents.get(issue.file, "")
            user_msg = (
                f"## 问题\n{issue.title}\n{issue.description}\n\n"
                f"## 文件 {issue.file} 当前内容\n```\n{file_content[:3000]}\n```\n\n"
                f"## 建议修复策略\n{issue.suggested_fix.strategy if issue.suggested_fix else 'N/A'}\n"
            )
            if retry > 0:
                user_msg += f"\n注意：这是第 {retry + 1} 次尝试，上次生成的 patch 验证失败，请调整。"
            resp: LLMResponse = llm.chat(PATCH_GENERATOR_SYSTEM, user_msg, json_mode=True, temperature=0.1)
            data = resp.parse_json()
            return data.get("diff", "")
        except LLMError as exc:
            self.logger.warning("patch_generator.llm_failed", error=str(exc), retry=retry)
            return ""
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("patch_generator.llm_error", error=str(exc), retry=retry)
            return ""

    def _check_constraints(self, diff: str, issue: Issue) -> bool:
        """检查 patch 是否满足约束（SPEC 10.2）。"""
        cfg = get_config().patch
        # 检查改动文件数（只允许 issue 相关文件）
        changed_files = self._extract_changed_files(diff)
        if issue.file and issue.file not in changed_files and len(changed_files) > 0:
            # 允许修改 issue 文件 + 相关测试文件
            non_related = [f for f in changed_files if f != issue.file
                           and not (issue.file and f.replace("test_", "").replace("_test", "")
                                    in issue.file)]
            if non_related and not cfg.allow_public_api_changes:
                self.logger.warning("patch_generator.modifies_unrelated_files", files=non_related)
                return False
        # 检查是否引入新依赖
        if any("requirements.txt" in f or "package.json" in f or "go.mod" in f
               for f in changed_files):
            if not cfg.allow_new_dependencies:
                return False
        # 检查 diff 规模（拒绝过大 patch）
        diff_lines = diff.count("\n")
        if diff_lines > 200:
            self.logger.warning("patch_generator.too_large", lines=diff_lines)
            return False
        return True

    @staticmethod
    def _extract_changed_files(diff: str) -> list[str]:
        files = []
        for line in diff.splitlines():
            if line.startswith("+++ b/"):
                files.append(line[6:])
            elif line.startswith("--- a/"):
                path = line[6:]
                if path != "/dev/null" and path not in files:
                    files.append(path)
        return files

    def _validate(self, test_commands: list[str]) -> PatchValidation:
        """运行验证命令：patch apply check + target test + lint + type check。"""
        cfg = get_config().patch
        if not cfg.publish_only_if_validated:
            return PatchValidation(status=VerificationStatus.SKIPPED, summary="配置跳过验证")

        commands_run: list[str] = []
        all_passed = True
        partial = False
        summaries: list[str] = []

        for cmd in test_commands[:5]:  # 限制验证命令数
            self.check_cancelled()
            result = self.tool("sandbox.exec", sandbox_id=self.sandbox_id,
                               command=cmd, timeout=300)
            commands_run.append(cmd)
            exit_code = result.exit_code if hasattr(result, "exit_code") else result.get("exit_code", -1)
            if exit_code != 0:
                all_passed = False
                stderr = result.stderr if hasattr(result, "stderr") else result.get("stderr", "")
                summaries.append(f"FAIL: {cmd}\n{stderr[:200]}")
            else:
                summaries.append(f"PASS: {cmd}")
                partial = True

        if all_passed and commands_run:
            status = VerificationStatus.PASSED
        elif not commands_run:
            status = VerificationStatus.SKIPPED
        elif partial:
            status = VerificationStatus.PARTIAL
        else:
            status = VerificationStatus.FAILED

        return PatchValidation(
            status=status,
            commands=commands_run,
            summary="\n".join(summaries),
        )
