"""Orchestrator —— 审查生命周期控制（SPEC 7.1 / 第 4、5 节）。

职责：
- 接收任务并控制状态机
- 调度 Planner 和子 Agent
- 处理中断、超时、取消和重试
- 聚合结果，决定是否生成 patch
- 发布最终审查结果

幂等规则（SPEC 4.3）：
- repository_id + pr_number + head_sha 唯一
- 重复 webhook → 返回已有任务
- 新 head SHA → 旧任务 SUPERSEDED，销毁 sandbox，新任务开始

Orchestrator 不直接执行代码，不直接生成 patch；只调度工具和 Agent。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from app.agents import (
    AgentCancelled,
    BugHunter,
    HeadShaMismatch,
    IssueAggregator,
    PatchGenerator,
    Planner,
    ReviewPublisher,
    SecurityReviewer,
    TestExecutor,
)
from app.blackboard import get_blackboard
from app.config import get_config
from app.db.repositories import IssueRepository, PatchRepository, ReviewTaskRepository
from app.db.session import session_scope
from app.logging_setup import get_logger
from app.models import (
    TERMINAL_STATES,
    Issue,
    Patch,
    ReviewPlan,
    ReviewState,
    ReviewTask,
    WebhookEvent,
)
from app.orchestrator.state_machine import StateMachine, StateMachineError
from app.tools import register_tools
from app.tools.gateway import ToolGateway, get_gateway
from app.tools.github_tool import get_github_client
from app.tools.sandbox_tool import get_sandbox_manager

logger = get_logger(__name__)


def make_review_id(repo_id: int, pr_number: int, head_sha: str) -> str:
    short_sha = head_sha[:12]
    return f"repo-{repo_id}-pr-{pr_number}-sha-{short_sha}"


class Orchestrator:
    """审查编排器：驱动完整审查流水线。"""

    def __init__(self, gateway: ToolGateway | None = None) -> None:
        self.gateway = gateway or get_gateway()
        register_tools(self.gateway)
        self._active_agents: dict[str, list[Any]] = {}  # review_id -> agents (for cancellation)
        self._sandbox_ids: dict[str, list[str]] = {}  # review_id -> sandbox ids
        self._processing: set[str] = set()  # 正在处理的 review_id（C4 并发去重）
        self._timed_out: set[str] = set()  # 已触发任务级超时的 review_id（H2）
        self._timers: dict[str, threading.Timer] = {}  # review_id -> 超时看门狗
        self._lock = threading.Lock()

    # ---------------- 入口：接收事件 ----------------

    def handle_event(self, event: WebhookEvent) -> ReviewTask:
        """处理 webhook 事件，返回任务（新建或已有）。"""
        review_id = make_review_id(event.repository_id, event.pr_number, event.head_sha)

        with session_scope() as session:
            repo = ReviewTaskRepository(session)
            # 1. 幂等：相同 task_key 已存在 → 返回已有
            existing = repo.get(review_id)
            if existing:
                logger.info("orchestrator.idempotent_existing", review_id=review_id,
                            status=existing.status.value)
                return existing

            # 2. SUPERSEDED：同 PR 有新 head SHA → 取消旧任务
            active_tasks = repo.find_active_for_pr(event.repository_id, event.pr_number)
            for old_task in active_tasks:
                if old_task.head_sha != event.head_sha:
                    self._supersede(old_task, session)

            # 3. 创建新任务
            task = ReviewTask(
                review_id=review_id,
                repository_id=event.repository_id,
                repository_full_name=event.repository_full_name,
                pr_number=event.pr_number,
                base_sha=event.base_sha,
                head_sha=event.head_sha,
                trigger=event.event_type,
            )
            task = repo.save(task)

        return task

    def process(self, review_id: str) -> ReviewTask:
        """执行完整审查流水线（同步）。"""
        with session_scope() as session:
            repo = ReviewTaskRepository(session)
            task = repo.get(review_id)
        if task is None:
            raise ValueError(f"未知 review_id: {review_id}")
        if task.status in {ReviewState.COMPLETED, ReviewState.SKIPPED}:
            return task
        if task.status in {ReviewState.SUPERSEDED, ReviewState.CANCELLED,
                           ReviewState.FAILED, ReviewState.TIMEOUT}:
            return task

        # C4：同一 review_id 已在处理中 → 幂等返回，避免并发双跑（重复评论/沙箱）。
        with self._lock:
            if review_id in self._processing:
                logger.info("orchestrator.already_processing", review_id=review_id)
                return task
            self._processing.add(review_id)

        # H2：任务级超时看门狗——到期取消所有 Agent 并标记超时。
        cfg = get_config()
        deadline = max(cfg.sandbox.default_timeout_seconds, cfg.agents.agent_timeout_seconds) + 120

        def _on_timeout() -> None:
            with self._lock:
                self._timed_out.add(review_id)
            logger.warning("orchestrator.task_timeout", review_id=review_id, deadline_s=deadline)
            self._cancel_agents(review_id)

        timer = threading.Timer(deadline, _on_timeout)
        timer.daemon = True
        timer.start()
        with self._lock:
            self._timers[review_id] = timer

        try:
            return self._run_pipeline(task)
        except AgentCancelled:
            # 区分超时取消 / 被取代 / 主动取消，避免把 SUPERSEDED 误标为 CANCELLED（H3）。
            with self._lock:
                was_timeout = review_id in self._timed_out
            latest = self._reload(review_id) or task
            if latest.status in {ReviewState.SUPERSEDED, ReviewState.CANCELLED,
                                 ReviewState.FAILED, ReviewState.TIMEOUT}:
                return latest  # 终态已由取代/取消方设定，不覆写
            if was_timeout:
                return self._fail(task, ReviewState.TIMEOUT, "任务超时")
            return self._fail(task, ReviewState.CANCELLED, "任务被取消")
        except HeadShaMismatch:
            return self._supersede_and_reload(task)
        except Exception as exc:  # noqa: BLE001
            logger.error("orchestrator.pipeline_failed", review_id=review_id, error=str(exc))
            return self._fail(task, ReviewState.FAILED, str(exc))
        finally:
            with self._lock:
                self._processing.discard(review_id)
                self._timed_out.discard(review_id)
                t = self._timers.pop(review_id, None)
            if t is not None:
                t.cancel()
            self._cleanup(task.review_id)

    # ---------------- 流水线 ----------------

    def _run_pipeline(self, task: ReviewTask) -> ReviewTask:
        review_id = task.review_id
        cfg = get_config()

        # VALIDATING_EVENT
        task = self._transition(task, ReviewState.VALIDATING_EVENT)
        self._validate_event(task)

        # PREPROCESSING
        task = self._transition(task, ReviewState.PREPROCESSING)
        context = self._preprocess(task)

        # PLANNING
        task = self._transition(task, ReviewState.PLANNING)
        plan = self._plan(task, context)

        if plan.skip_reason:
            task = self._transition(task, ReviewState.SKIPPED)
            logger.info("orchestrator.skipped", review_id=review_id, reason=plan.skip_reason)
            return self._finalize(task, plan=plan)

        sandbox_id: str | None = None
        if plan.sandbox_required and cfg.sandbox.enabled:
            # SANDBOX_PREPARE
            task = self._transition(task, ReviewState.SANDBOX_PREPARE)
            sandbox_id = self._prepare_sandbox(task, context)
            if sandbox_id is None:
                logger.warning("orchestrator.sandbox_unavailable_continue_static", review_id=review_id)

        # STATIC_REVIEW
        task = self._transition(task, ReviewState.STATIC_REVIEW)
        static_issues = self._static_review(task, context, plan, sandbox_id)

        # DYNAMIC_TESTING
        test_results: dict[str, Any] | None = None
        if sandbox_id:
            task = self._transition(task, ReviewState.DYNAMIC_TESTING)
            test_results = self._dynamic_testing(task, context, plan, sandbox_id, static_issues)
        else:
            logger.info("orchestrator.skip_dynamic_no_sandbox", review_id=review_id)

        # ISSUE_AGGREGATION
        task = self._transition(task, ReviewState.ISSUE_AGGREGATION)
        all_issues = static_issues + (test_results.get("issues", []) if test_results else [])
        agg_result = self._aggregate(task, all_issues)

        # PATCH_GENERATION + PATCH_VALIDATION
        patches: list[Patch] = []
        if agg_result["should_generate_patch"] and sandbox_id:
            task = self._transition(task, ReviewState.PATCH_GENERATION)
            patches = self._generate_patches(task, agg_result["patch_candidates"],
                                              context, test_results)
            task = self._transition(task, ReviewState.PATCH_VALIDATION)
            # 验证已在 generate 中完成
        else:
            task = self._transition(task, ReviewState.REVIEW_PUBLISHING)

        # REVIEW_PUBLISHING
        if task.status == ReviewState.PATCH_VALIDATION:
            task = self._transition(task, ReviewState.REVIEW_PUBLISHING)
        self._publish(task, agg_result["issues"], patches, plan, test_results)

        # COMPLETED
        task = self._transition(task, ReviewState.COMPLETED)
        return self._finalize(task, plan=plan, issues=agg_result["issues"], patches=patches,
                              test_results=test_results)

    # ---------------- 各阶段实现 ----------------

    def _validate_event(self, task: ReviewTask) -> None:
        """验证事件类型与权限。"""
        cfg = get_config().review
        if task.trigger not in cfg.trigger_events:
            raise ValueError(f"不支持的触发事件: {task.trigger}")

    def _preprocess(self, task: ReviewTask) -> dict[str, Any]:
        """获取 PR 元数据、diff、变更文件（SPEC 5.1）。"""
        repo = task.repository_full_name
        pr = self.gateway.call("github.get_pr", review_id=task.review_id, caller="Orchestrator",
                               head_sha=task.head_sha,
                               params={"repo": repo, "pr_number": task.pr_number})
        changed_files = self.gateway.call("github.get_changed_files", review_id=task.review_id,
                                          caller="Orchestrator", head_sha=task.head_sha,
                                          params={"repo": repo, "pr_number": task.pr_number})
        diff = self.gateway.call("github.get_diff", review_id=task.review_id,
                                 caller="Orchestrator", head_sha=task.head_sha,
                                 params={"repo": repo, "pr_number": task.pr_number})

        # 获取变更文件完整内容（head_sha 版本）
        file_contents: dict[str, str] = {}
        for f in changed_files:
            path = f["path"]
            if f.get("status") == "removed":
                continue
            try:
                content = self.gateway.call("github.get_file_content", review_id=task.review_id,
                                            caller="Orchestrator", head_sha=task.head_sha,
                                            params={"repo": repo, "ref": task.head_sha, "path": path})
                file_contents[path] = content
            except Exception as exc:  # noqa: BLE001
                logger.warning("orchestrator.fetch_file_failed", path=path, error=str(exc))

        logger.info("orchestrator.preprocessed", review_id=task.review_id,
                    files=len(changed_files), diff_lines=len(diff))
        return {
            "pr": pr,
            "changed_files": changed_files,
            "diff": diff,
            "file_contents": file_contents,
            "repo_files": list(file_contents.keys()),
        }

    def _plan(self, task: ReviewTask, context: dict[str, Any]) -> ReviewPlan:
        planner = Planner(
            review_id=task.review_id, head_sha=task.head_sha, base_sha=task.base_sha,
            repository_full_name=task.repository_full_name, pr_number=task.pr_number,
            gateway=self.gateway,
        )
        self._register_agent(task.review_id, planner)
        plan = planner.run(
            changed_files=context["changed_files"],
            pr_title=context["pr"].get("title", ""),
            pr_description=context["pr"].get("body", ""),
            repo_files=context["repo_files"],
        )
        return plan

    def _prepare_sandbox(self, task: ReviewTask, context: dict[str, Any], ref: str | None = None) -> str | None:
        """创建并准备沙箱工作区。"""
        cfg = get_config().sandbox
        ref = ref or task.head_sha
        try:
            sandbox_id = self.gateway.call(
                "sandbox.create", review_id=task.review_id, caller="Orchestrator",
                head_sha=task.head_sha,
                params={"review_id": task.review_id, "image": cfg.image,
                        "network_policy": "disabled"},
            )
            self._register_sandbox(task.review_id, sandbox_id)

            # 下载代码归档（token 在容器外使用）
            gh = get_github_client()
            import httpx

            archive_url = f"https://api.github.com/repos/{task.repository_full_name}/tarball/{ref}"
            token = gh._get_installation_token(task.repository_full_name)  # noqa: SLF001
            resp = httpx.get(archive_url, headers={"Authorization": f"token {token}",
                                                    "Accept": "application/vnd.github+json"},
                             follow_redirects=True, timeout=120.0)
            archive = resp.content if resp.status_code == 200 else None

            self.gateway.call("sandbox.prepare_workspace", review_id=task.review_id,
                              caller="Orchestrator", head_sha=ref,
                              params={"sandbox_id": sandbox_id, "repo": task.repository_full_name,
                                      "base_sha": task.base_sha, "head_sha": ref,
                                      "code_archive": archive})
            return sandbox_id
        except Exception as exc:  # noqa: BLE001
            logger.error("orchestrator.sandbox_prepare_failed", error=str(exc))
            return None

    def _static_review(self, task: ReviewTask, context: dict[str, Any],
                       plan: ReviewPlan, sandbox_id: str | None = None) -> list[Issue]:
        """并行运行 SecurityReviewer 和 BugHunter。"""
        cfg = get_config().agents
        issues: list[Issue] = []
        agents_to_run: list[Any] = []

        for t in plan.tasks:
            if t.agent == "SecurityReviewer":
                agents_to_run.append(SecurityReviewer(
                    review_id=task.review_id, head_sha=task.head_sha, gateway=self.gateway))
            elif t.agent == "BugHunter":
                agents_to_run.append(BugHunter(
                    review_id=task.review_id, head_sha=task.head_sha, gateway=self.gateway))

        for agent in agents_to_run:
            self._register_agent(task.review_id, agent)

        # M2：若有沙箱，先在沙箱中运行真实 SAST（bandit），把结果作为
        # "已由工具确认"的证据喂给 SecurityReviewer（使"已验证漏洞"层可达）。
        sast_findings = self._run_sast(sandbox_id, context, task.review_id) if sandbox_id else []

        with ThreadPoolExecutor(max_workers=cfg.max_parallel_agents) as pool:
            futures = {}
            for agent in agents_to_run:
                if isinstance(agent, SecurityReviewer):
                    fut = pool.submit(
                        agent.run, context["changed_files"], context["file_contents"],
                        context["diff"], sast_findings,
                    )
                else:
                    fut = pool.submit(
                        agent.run, context["changed_files"], context["file_contents"], context["diff"]
                    )
                futures[fut] = agent.name
            for future in as_completed(futures):
                name = futures[future]
                try:
                    result = future.result()
                    issues.extend(result)
                except AgentCancelled:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.warning("orchestrator.agent_failed", agent=name, error=str(exc))

        # 持久化 issues
        with session_scope() as session:
            issue_repo = IssueRepository(session)
            for issue in issues:
                issue_repo.save(issue)
        return issues

    def _run_sast(self, sandbox_id: str, context: dict[str, Any],
                  review_id: str) -> list[dict[str, Any]]:
        """在沙箱中运行真实 SAST（bandit JSON），解析为 findings（source=sast）。

        仅对变更的 Python 文件运行；失败/无 Python 时安全降级为空（SPEC 15.1/15.2）。
        """
        import json as _json

        py_files = [f["path"] for f in context.get("changed_files", [])
                    if isinstance(f, dict) and f.get("path", "").endswith(".py")
                    and f.get("status") != "removed"]
        if not py_files:
            return []
        targets = " ".join(f"'{p}'" for p in py_files)
        try:
            result = self.gateway.call(
                "sandbox.exec", review_id=review_id, caller="Orchestrator",
                params={"sandbox_id": sandbox_id, "command": f"bandit -f json -q {targets}",
                        "timeout": 300},
            )
            stdout = result.stdout if hasattr(result, "stdout") else result.get("stdout", "")
        except Exception as exc:  # noqa: BLE001
            logger.warning("orchestrator.sast_failed_degrade", error=str(exc))
            return []
        findings: list[dict[str, Any]] = []
        try:
            data = _json.loads(stdout) if stdout.strip() else {}
            for r in data.get("results", []):
                sev = {"HIGH": "high", "MEDIUM": "medium", "LOW": "low"}.get(
                    r.get("issue_severity", "MEDIUM"), "medium")
                conf_map = {"HIGH": 0.9, "MEDIUM": 0.8, "LOW": 0.65}
                findings.append({
                    "source": "sast",
                    "tool": "bandit",
                    "rule": r.get("test_id", ""),
                    "message": r.get("issue_text", ""),
                    "file": r.get("filename", ""),
                    "line": r.get("line_number"),
                    "severity": sev,
                    "confidence": conf_map.get(r.get("issue_confidence", "MEDIUM"), 0.8),
                })
        except (ValueError, TypeError) as exc:
            logger.warning("orchestrator.sast_parse_failed", error=str(exc))
            return []
        logger.info("orchestrator.sast_done", findings=len(findings))
        return findings

    def _dynamic_testing(self, task: ReviewTask, context: dict[str, Any],
                         plan: ReviewPlan, sandbox_id: str, static_issues: list[Issue]) -> dict[str, Any]:
        """Run the bounded test-engineer loop and compare failures against base."""
        executor = TestExecutor(
            review_id=task.review_id, head_sha=task.head_sha, sandbox_id=sandbox_id,
            repository_full_name=task.repository_full_name, base_sha=task.base_sha,
            gateway=self.gateway,
        )
        self._register_agent(task.review_id, executor)
        result = executor.run(context["changed_files"], static_issues=static_issues)

        # A failing head command is evidence, not a PR regression, until the same
        # command is tried on base in a fresh, equivalently prepared sandbox.
        cfg = get_config().testing
        if cfg.compare_base_on_failure and result.get("failed_commands"):
            base_sandbox_id = self._prepare_sandbox(task, context, ref=task.base_sha)
            if base_sandbox_id:
                base_executor = TestExecutor(
                    review_id=task.review_id, head_sha=task.base_sha, sandbox_id=base_sandbox_id,
                    repository_full_name=task.repository_full_name, base_sha=task.base_sha,
                    gateway=self.gateway,
                )
                self._register_agent(task.review_id, base_executor)
                base_results = base_executor.compare_failures(
                    result["failed_commands"], result.get("temporary_files", {})
                )
                executor.reconcile_baseline(result, base_results)
            else:
                result["report"].limitations.append("无法创建 base SHA sandbox，未完成回归归因")
        try:
            executor.tool("sandbox.reset_temp_files", sandbox_id=sandbox_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("orchestrator.reset_temp_tests_failed", error=str(exc))

        # 持久化测试失败 issues
        with session_scope() as session:
            issue_repo = IssueRepository(session)
            for issue in result.get("issues", []):
                issue_repo.save(issue)
        return result

    def _aggregate(self, task: ReviewTask, issues: list[Issue]) -> dict[str, Any]:
        aggregator = IssueAggregator(
            review_id=task.review_id, head_sha=task.head_sha, gateway=self.gateway)
        self._register_agent(task.review_id, aggregator)
        result = aggregator.run(issues)
        # 持久化聚合后的 issues（含 publication 状态）
        with session_scope() as session:
            issue_repo = IssueRepository(session)
            for issue in result["issues"] + result["suppressed"]:
                issue_repo.save(issue)
        return result

    def _generate_patches(self, task: ReviewTask, candidates: list[Issue],
                          context: dict[str, Any], test_results: dict[str, Any] | None) -> list[Patch]:
        sandbox_id = self._sandbox_ids.get(task.review_id, [None])[0] if self._sandbox_ids.get(task.review_id) else None
        gen = PatchGenerator(
            review_id=task.review_id, head_sha=task.head_sha, sandbox_id=sandbox_id,
            file_contents=context["file_contents"], gateway=self.gateway,
        )
        self._register_agent(task.review_id, gen)
        report = test_results.get("report") if test_results else None
        # Generated tests are deliberately ephemeral and have been removed after
        # attribution; patch validation only reruns durable repository commands.
        test_cmds = [r.command for r in report.commands if r.phase == "existing"] if report else []
        patches = gen.run(candidates, test_cmds)
        with session_scope() as session:
            patch_repo = PatchRepository(session)
            for p in patches:
                patch_repo.save(p)
        return patches

    def _publish(self, task: ReviewTask, issues: list[Issue], patches: list[Patch],
                 plan: ReviewPlan, test_results: dict[str, Any] | None) -> None:
        publisher = ReviewPublisher(
            review_id=task.review_id, head_sha=task.head_sha,
            repository_full_name=task.repository_full_name, pr_number=task.pr_number,
            gateway=self.gateway,
        )
        self._register_agent(task.review_id, publisher)
        publisher.run(task, issues, patches, plan, test_results)

    # ---------------- 状态机辅助 ----------------

    def _reload(self, review_id: str) -> ReviewTask | None:
        with session_scope() as session:
            return ReviewTaskRepository(session).get(review_id)

    def _transition(self, task: ReviewTask, target: ReviewState) -> ReviewTask:
        # H3：若 DB 中该任务已被外部置为终态（如被新 commit 取代为 SUPERSEDED，
        # 或被取消/超时），旧流水线必须中止，绝不用内存状态覆写终态。
        current = self._reload(task.review_id)
        if current is not None and current.status in TERMINAL_STATES:
            logger.info("orchestrator.transition_aborted_terminal", review_id=task.review_id,
                        db_state=current.status.value, attempted=target.value)
            raise AgentCancelled(
                f"任务已进入终态 {current.status.value}，中止流水线 (review={task.review_id})"
            )
        try:
            StateMachine.transition(task, target)
        except StateMachineError as exc:
            logger.error("orchestrator.transition_failed", review_id=task.review_id,
                         current=task.status.value, target=target.value, error=str(exc))
            raise
        with session_scope() as session:
            task = ReviewTaskRepository(session).save(task)
        logger.info("orchestrator.transition", review_id=task.review_id, state=target.value)
        return task

    def _finalize(self, task: ReviewTask, plan: ReviewPlan | None = None,
                  issues: list[Issue] | None = None, patches: list[Patch] | None = None,
                  test_results: dict[str, Any] | None = None) -> ReviewTask:
        with session_scope() as session:
            repo = ReviewTaskRepository(session)
            task = repo.get(task.review_id) or task
            if test_results and test_results.get("report"):
                task.test_report = test_results["report"]
            if task.status not in {ReviewState.COMPLETED, ReviewState.SKIPPED,
                                   ReviewState.SUPERSEDED, ReviewState.CANCELLED,
                                   ReviewState.FAILED, ReviewState.TIMEOUT}:
                task.transition(ReviewState.COMPLETED)
            task = repo.save(task)
        logger.info("orchestrator.finalized", review_id=task.review_id, status=task.status.value)
        return task

    def _fail(self, task: ReviewTask, state: ReviewState, message: str) -> ReviewTask:
        task.error = message
        try:
            StateMachine.transition(task, state, force=True)
        except StateMachineError:
            task.status = state
        with session_scope() as session:
            task = ReviewTaskRepository(session).save(task)
        logger.warning("orchestrator.failed", review_id=task.review_id,
                       state=state.value, error=message)
        self._cleanup(task.review_id)
        return task

    def _supersede(self, task: ReviewTask, session) -> None:
        """将旧任务标记为 SUPERSEDED 并取消其 Agent 与沙箱。"""
        try:
            StateMachine.transition(task, ReviewState.SUPERSEDED, force=True)
        except StateMachineError:
            task.status = ReviewState.SUPERSEDED
        ReviewTaskRepository(session).save(task)
        self._cancel_agents(task.review_id)
        self._cleanup(task.review_id)
        logger.info("orchestrator.superseded", review_id=task.review_id,
                    head_sha=task.head_sha[:8])

    def _supersede_and_reload(self, task: ReviewTask) -> ReviewTask:
        with session_scope() as session:
            repo = ReviewTaskRepository(session)
            t = repo.get(task.review_id) or task
            try:
                StateMachine.transition(t, ReviewState.SUPERSEDED, force=True)
            except StateMachineError:
                t.status = ReviewState.SUPERSEDED
            t = repo.save(t)
        self._cancel_agents(task.review_id)
        self._cleanup(task.review_id)
        return t

    # ---------------- 取消与清理 ----------------

    def _register_agent(self, review_id: str, agent: Any) -> None:
        with self._lock:
            self._active_agents.setdefault(review_id, []).append(agent)

    def _register_sandbox(self, review_id: str, sandbox_id: str) -> None:
        with self._lock:
            self._sandbox_ids.setdefault(review_id, []).append(sandbox_id)

    def _cancel_agents(self, review_id: str) -> None:
        with self._lock:
            agents = self._active_agents.pop(review_id, [])
        for agent in agents:
            try:
                if hasattr(agent, "cancel"):
                    agent.cancel()
            except Exception:  # noqa: BLE001
                pass

    def _cleanup(self, review_id: str) -> None:
        """销毁相关沙箱（SPEC 2.5）。"""
        with self._lock:
            sandbox_ids = self._sandbox_ids.pop(review_id, [])
        sm = get_sandbox_manager()
        for sid in sandbox_ids:
            try:
                sm.destroy(sid)
            except Exception as exc:  # noqa: BLE001
                logger.warning("orchestrator.cleanup_sandbox_failed", sandbox_id=sid, error=str(exc))
        # 清理 blackboard（内存）
        try:
            get_blackboard().clear(review_id)
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 辅助 ----------------

    @staticmethod
    def _detect_install_commands(repo_files: list[str]) -> list[str]:
        file_set = {f.lower() for f in repo_files}
        cmds: list[str] = []
        if "requirements.txt" in file_set:
            cmds.append("pip install -r requirements.txt")
        if "pyproject.toml" in file_set:
            cmds.append("pip install -e . || pip install -r requirements.txt || true")
        if "package.json" in file_set:
            cmds.append("npm ci || npm install")
        if "go.mod" in file_set:
            cmds.append("go mod download")
        return cmds


_default_orchestrator: Orchestrator | None = None


def get_orchestrator() -> Orchestrator:
    global _default_orchestrator
    if _default_orchestrator is None:
        _default_orchestrator = Orchestrator()
    return _default_orchestrator


def reset_orchestrator() -> None:
    global _default_orchestrator
    _default_orchestrator = None
