"""命令行入口 —— 启动服务、手动触发审查、初始化数据库。"""

from __future__ import annotations

import json
import sys

import click

from app.config import get_settings
from app.db.session import init_db
from app.logging_setup import configure_logging
from app.models import WebhookEvent
from app.orchestrator import get_orchestrator
from app.tools import register_tools


@click.group()
def main() -> None:
    """PR Review Agent 命令行工具。"""
    configure_logging()


@main.command()
@click.option("--host", default=None, help="监听地址")
@click.option("--port", default=None, type=int, help="监听端口")
@click.option("--reload/--no-reload", default=False, help="热重载（开发）")
def serve(host: str | None, port: int | None, reload: bool) -> None:
    """启动 Webhook Receiver 服务。"""
    settings = get_settings()
    host = host or settings.app_host
    port = port or settings.app_port
    init_db()
    register_tools()
    import uvicorn

    uvicorn.run("app.main:app", host=host, port=port, reload=reload)


@main.command()
def initdb() -> None:
    """初始化数据库表。"""
    init_db()
    click.echo("数据库表已创建。")


@main.command()
@click.option("--repo", required=True, help="仓库全名 org/repo")
@click.option("--pr", required=True, type=int, help="PR 编号")
@click.option("--sha", required=True, help="head SHA")
@click.option("--base-sha", default="", help="base SHA")
@click.option("--trigger", default="pull_request.opened", help="触发事件")
def review(repo: str, pr: int, sha: str, base_sha: str, trigger: str) -> None:
    """手动触发一次 PR 审查。"""
    register_tools()
    # 解析 repository_id（从仓库名查询）
    from app.tools.github_tool import get_github_client

    gh = get_github_client()
    try:
        pr_data = gh.get_pr(repo, pr)
        head_sha = sha or pr_data["head_sha"]
        base = base_sha or pr_data["base_sha"]
        repo_id = abs(hash(repo)) % (10 ** 10)  # 简化：用 hash 模拟 id
    except Exception as exc:  # noqa: BLE001
        click.echo(f"获取 PR 信息失败: {exc}", err=True)
        sys.exit(1)

    event = WebhookEvent(
        event_type=trigger,
        action=trigger.split(".")[-1],
        repository_id=repo_id,
        repository_full_name=repo,
        pr_number=pr,
        base_sha=base,
        head_sha=head_sha,
    )
    orchestrator = get_orchestrator()
    task = orchestrator.handle_event(event)
    click.echo(f"任务已创建: {task.review_id} (状态: {task.status.value})")
    click.echo("开始执行审查...")
    task = orchestrator.process(task.review_id)
    click.echo(f"\n审查完成: 状态={task.status.value}")
    if task.error:
        click.echo(f"错误: {task.error}")


@main.command()
@click.option("--review-id", required=True, help="review_id")
def status(review_id: str) -> None:
    """查询审查任务状态。"""
    from app.db.repositories import IssueRepository, PatchRepository, ReviewTaskRepository
    from app.db.session import session_scope

    with session_scope() as session:
        task = ReviewTaskRepository(session).get(review_id)
        if task is None:
            click.echo("未找到任务", err=True)
            sys.exit(1)
        issues = IssueRepository(session).list_for_review(review_id)
        patches = PatchRepository(session).list_for_review(review_id)

    click.echo(json.dumps({
        "review_id": task.review_id,
        "status": task.status.value,
        "repository": task.repository_full_name,
        "pr_number": task.pr_number,
        "head_sha": task.head_sha[:12],
        "issues": len(issues),
        "patches": len(patches),
        "error": task.error,
    }, indent=2, ensure_ascii=False))


@main.command()
def config() -> None:
    """显示当前配置。"""
    from app.config import get_config

    cfg = get_config()
    click.echo(json.dumps(cfg.model_dump(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
