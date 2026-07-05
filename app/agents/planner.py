"""Planner —— 审查计划生成（SPEC 7.2 / 5.2）。

分析 PR diff，识别语言/框架，判断风险区域，生成任务图，
决定是否需要 sandbox 及哪些任务可并行。
"""

from __future__ import annotations

import uuid
from typing import Any

from app.agents.base import BaseAgent
from app.config import get_config
from app.llm import LLMError, LLMResponse, get_llm_client
from app.llm.prompts import PLANNER_SYSTEM
from app.models import (
    ChangeType,
    PlannedFile,
    PlannedTask,
    PrChangeCategory,
    ReviewPlan,
    RiskLevel,
)
from app.tools.analyzer_tool import get_analyzer


class Planner(BaseAgent):
    name = "Planner"

    def __init__(self, review_id: str, head_sha: str, base_sha: str | None = None,
                 repository_full_name: str = "", pr_number: int = 0, **kwargs: Any) -> None:
        super().__init__(review_id, head_sha, **kwargs)
        self.base_sha = base_sha
        self.repository_full_name = repository_full_name
        self.pr_number = pr_number

    def run(
        self,
        changed_files: list[dict[str, Any]],
        pr_title: str = "",
        pr_description: str = "",
        repo_files: list[str] | None = None,
    ) -> ReviewPlan:
        self.check_cancelled()
        analyzer = get_analyzer()
        cfg = get_config().review

        # 1. 判断是否文档/纯无意义变更 → SKIPPED
        categories = analyzer.classify_pr(changed_files)
        is_docs_only = analyzer.is_docs_only(changed_files)

        if is_docs_only and cfg.skip_docs_only:
            return ReviewPlan(
                review_id=self.review_id,
                head_sha=self.head_sha,
                base_sha=self.base_sha,
                language_profile=[],
                change_categories=categories,
                changed_files=[],
                tasks=[],
                sandbox_required=False,
                estimated_cost_class="low",
                skip_reason="仅文档变更，跳过深度审查",
            )

        # 2. 变更规模检查
        if len(changed_files) > cfg.max_changed_files:
            return ReviewPlan(
                review_id=self.review_id,
                head_sha=self.head_sha,
                base_sha=self.base_sha,
                language_profile=analyzer.detect_language(changed_files),
                change_categories=categories,
                changed_files=[],
                tasks=[],
                sandbox_required=False,
                estimated_cost_class="high",
                skip_reason=f"变更文件数 {len(changed_files)} 超过上限 {cfg.max_changed_files}",
            )

        # 3. 语言识别
        language_profile = analyzer.detect_language(changed_files)

        # 4. 风险评估
        risk_assessment = analyzer.assess_risk(changed_files)
        planned_files = [
            PlannedFile(
                path=r["path"],
                change_type=ChangeType(r["change_type"]) if r["change_type"] in [e.value for e in ChangeType] else ChangeType.MODIFIED,
                risk_level=RiskLevel(r["risk_level"]),
                reasons=r.get("reasons", []),
            )
            for r in risk_assessment
        ]

        # 5. 沙箱需求：有代码变更即需要沙箱以运行动态测试/lint/SAST/patch 验证。
        has_code = PrChangeCategory.CODE.value in categories
        sandbox_required = has_code

        # 6. 生成任务图
        tasks = self._build_tasks(planned_files, categories, language_profile)

        # 7. 尝试用 LLM 增强计划（可选，失败则用规则计划）
        llm_plan = self._try_llm_plan(changed_files, pr_title, pr_description, language_profile)
        if llm_plan:
            tasks = self._merge_llm_tasks(tasks, llm_plan)

        # 8. 成本估算
        cost = "low"
        if any(t.priority == "high" for t in tasks):
            cost = "medium"
        if sandbox_required and len(tasks) >= 3:
            cost = "high"

        return ReviewPlan(
            review_id=self.review_id,
            head_sha=self.head_sha,
            base_sha=self.base_sha,
            language_profile=language_profile,
            change_categories=categories,
            changed_files=planned_files,
            tasks=tasks,
            sandbox_required=sandbox_required,
            estimated_cost_class=cost,
        )

    def _build_tasks(
        self,
        files: list[PlannedFile],
        categories: list[str],
        languages: list[str],
    ) -> list[PlannedTask]:
        tasks: list[PlannedTask] = []
        high_risk_files = [f.path for f in files if f.risk_level == RiskLevel.HIGH]
        code_files = [f.path for f in files if any(
            f.path.endswith(ext) for ext in (".py", ".js", ".jsx", ".ts", ".tsx", ".go")
        )]

        if PrChangeCategory.SECURITY_SENSITIVE.value in categories or high_risk_files:
            tasks.append(PlannedTask(
                id=f"task-sec-{uuid.uuid4().hex[:8]}",
                agent="SecurityReviewer",
                target_files=high_risk_files or code_files,
                focus=["authentication", "injection", "authorization", "secret-leakage"],
                priority="high",
                depends_on=[],
            ))

        if code_files:
            tasks.append(PlannedTask(
                id=f"task-bug-{uuid.uuid4().hex[:8]}",
                agent="BugHunter",
                target_files=code_files,
                focus=["null-handling", "boundary", "error-handling", "race-condition"],
                priority="medium",
                depends_on=[],
            ))

        if PrChangeCategory.TEST.value in categories or code_files:
            test_files = [f.path for f in files if "test" in f.path.lower()]
            tasks.append(PlannedTask(
                id=f"task-test-{uuid.uuid4().hex[:8]}",
                agent="TestExecutor",
                target_files=test_files or code_files,
                focus=["regression"],
                priority="high" if code_files else "medium",
                depends_on=[],
            ))

        return tasks

    def _try_llm_plan(
        self,
        changed_files: list[dict[str, Any]],
        pr_title: str,
        pr_description: str,
        language_profile: list[str],
    ) -> dict[str, Any] | None:
        """尝试用 LLM 增强计划；失败则返回 None（降级为纯规则计划）。"""
        try:
            llm = get_llm_client()
            diff_summary = self._summarize_files(changed_files)
            user_msg = (
                f"PR 标题: {pr_title}\n"
                f"PR 描述: {pr_description[:500]}\n"
                f"语言: {language_profile}\n"
                f"变更文件:\n{diff_summary}"
            )
            resp: LLMResponse = llm.chat(PLANNER_SYSTEM, user_msg, json_mode=True, temperature=0.1)
            return resp.parse_json()
        except LLMError as exc:
            self.logger.warning("planner.llm_failed_fallback_rules", error=str(exc))
            return None
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("planner.llm_error", error=str(exc))
            return None

    @staticmethod
    def _summarize_files(changed_files: list[dict[str, Any]]) -> str:
        lines = []
        for f in changed_files[:50]:
            path = f.get("path", "")
            status = f.get("status", "modified")
            lines.append(f"- {status} {path} (+{f.get('additions',0)}/-{f.get('deletions',0)})")
        return "\n".join(lines)

    def _merge_llm_tasks(self, rule_tasks: list[PlannedTask], llm_plan: dict[str, Any]) -> list[PlannedTask]:
        """合并 LLM 计划与规则计划，优先 LLM 但保留规则兜底。"""
        llm_tasks = llm_plan.get("tasks", [])
        if not llm_tasks:
            return rule_tasks
        merged: list[PlannedTask] = []
        seen_agents: set[str] = set()
        for t in llm_tasks:
            try:
                merged.append(PlannedTask(
                    id=t.get("id", f"task-llm-{uuid.uuid4().hex[:8]}"),
                    agent=t.get("agent", "Unknown"),
                    target_files=t.get("target_files", []),
                    focus=t.get("focus", []),
                    priority=t.get("priority", "medium"),
                    depends_on=t.get("depends_on", []),
                ))
                seen_agents.add(t.get("agent", ""))
            except Exception:  # noqa: BLE001
                continue
        # 补充规则任务中 LLM 未覆盖的
        for t in rule_tasks:
            if t.agent not in seen_agents:
                merged.append(t)
        return merged
