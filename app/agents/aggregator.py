"""Issue Aggregator —— 发现聚合（SPEC 7 / 5.5）。

职责：
1. 合并重复发现
2. 移除无证据低置信度问题
3. 标记已验证与未验证问题
4. 按严重性、置信度和影响范围排序
5. 判断是否进入 patch 阶段
"""

from __future__ import annotations

from typing import Any

from app.agents.base import BaseAgent
from app.config import get_config
from app.models import Issue, PublicationStatus, Severity, VerificationStatus

# 低置信度阈值：低于此值且无运行时/工具佐证的发现将被抑制（除非配置允许发布）。
_LOW_CONFIDENCE_THRESHOLD = 0.5

# 严重等级排序权重
_SEVERITY_ORDER = {
    Severity.CRITICAL: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
    Severity.INFO: 4,
}


class IssueAggregator(BaseAgent):
    name = "IssueAggregator"

    def __init__(self, review_id: str, head_sha: str, **kwargs: Any) -> None:
        super().__init__(review_id, head_sha, **kwargs)

    def run(self, issues: list[Issue]) -> dict[str, Any]:
        self.check_cancelled()
        cfg = get_config()

        # 1. 合并重复发现（同文件、同行、同类型）
        deduped = self._deduplicate(issues)

        # 2. 过滤低置信度且缺乏佐证的问题。
        #    佐证仅指可核验的执行/工具输出（runtime/tool_outputs）；diff_hunk、
        #    related_code 只是定位上下文，任何 issue 都会带，不能用来"拯救"低置信度
        #    发现（修复 M3 中过滤永不触发的死逻辑）。
        filtered = []
        suppressed = []
        for issue in deduped:
            has_corroboration = bool(issue.evidence.runtime_outputs or issue.evidence.tool_outputs)
            if issue.confidence < _LOW_CONFIDENCE_THRESHOLD and not has_corroboration:
                if not cfg.review.publish_low_confidence_findings:
                    issue.publication.status = PublicationStatus.SUPPRESSED
                    suppressed.append(issue)
                    continue
            filtered.append(issue)

        # 3. 标记验证状态。注意语义：VerificationStatus.PASSED 表示"验证通过"，
        #    而失败的测试/超时并不代表通过——它们的 runtime_output 恰恰是失败证据。
        #    因此只有当存在 exit_code==0 的 runtime 输出（真正通过的核验）才标 PASSED；
        #    已由生产者设定的确定性状态（如 TIMEOUT/FAILED）予以保留（修复 M3 误标）。
        for issue in filtered:
            if issue.verification.status in {
                VerificationStatus.TIMEOUT,
                VerificationStatus.FAILED,
                VerificationStatus.PARTIAL,
                VerificationStatus.SKIPPED,
            }:
                continue  # 保留生产者已设定的确定性状态
            passed = any(r.exit_code == 0 for r in issue.evidence.runtime_outputs)
            failed = any(r.exit_code not in (0, None) for r in issue.evidence.runtime_outputs)
            if passed:
                issue.verification.status = VerificationStatus.PASSED
            elif failed:
                issue.verification.status = VerificationStatus.FAILED
            else:
                issue.verification.status = VerificationStatus.NOT_RUN

        # 4. 排序：严重性 → 置信度 → 是否有可核验的运行时证据
        filtered.sort(key=lambda i: (
            _SEVERITY_ORDER.get(i.severity, 9),
            -i.confidence,
            0 if i.evidence.runtime_outputs else 1,
        ))

        # 5. 判断是否进入 patch 阶段
        patch_candidates = [
            i for i in filtered
            if i.meets_patch_criteria(cfg.patch.min_confidence_for_patch)
            and cfg.review.enable_auto_patch
        ]

        self.logger.info("aggregator.done",
                         input=len(issues), deduped=len(deduped),
                         output=len(filtered), suppressed=len(suppressed),
                         patch_candidates=len(patch_candidates))

        return {
            "issues": filtered,
            "suppressed": suppressed,
            "patch_candidates": patch_candidates,
            "should_generate_patch": len(patch_candidates) > 0,
        }

    def _deduplicate(self, issues: list[Issue]) -> list[Issue]:
        """合并重复发现：相同 file + type + 行范围（±3行）视为重复，保留置信度最高者。"""
        groups: dict[tuple[str, str, int], list[Issue]] = {}
        for issue in issues:
            # 行范围归一化（±3 行分组）
            line_bucket = (issue.line_start or 0) // 3 if issue.line_start else 0
            key = (issue.file, issue.type.value, line_bucket)
            groups.setdefault(key, []).append(issue)

        result = []
        for group in groups.values():
            if len(group) == 1:
                result.append(group[0])
                continue
            # 保留置信度最高的，合并证据
            group.sort(key=lambda i: -i.confidence)
            primary = group[0]
            for other in group[1:]:
                primary.evidence.tool_outputs.extend(other.evidence.tool_outputs)
                primary.evidence.runtime_outputs.extend(other.evidence.runtime_outputs)
                if other.evidence.diff_hunk and not primary.evidence.diff_hunk:
                    primary.evidence.diff_hunk = other.evidence.diff_hunk
            result.append(primary)
        return result
