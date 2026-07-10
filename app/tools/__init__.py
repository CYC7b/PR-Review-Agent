"""工具层导出与网关注册（SPEC 第 8 节）。

将所有工具函数注册到 ToolGateway，供 Agent 通过统一接口调用。
"""

from __future__ import annotations

from app.logging_setup import get_logger
from app.tools.analyzer_tool import AnalyzerTool, get_analyzer
from app.tools.gateway import ToolGateway, get_gateway, reset_gateway
from app.tools.github_tool import (
    GitHubAPIError,
    GitHubAuthError,
    GitHubClient,
    get_github_client,
    reset_github_client,
)
from app.tools.memory_tool import MemoryTool, get_memory_tool
from app.tools.sandbox_tool import (
    DockerSandboxManager,
    ExecResult,
    LocalSandboxManager,
    SandboxManager,
    get_sandbox_manager,
    reset_sandbox_manager,
)

logger = get_logger(__name__)

_gateway_registered = False


def register_tools(gateway: ToolGateway | None = None) -> ToolGateway:
    """将所有工具注册到网关。"""
    global _gateway_registered
    gateway = gateway or get_gateway()
    if _gateway_registered:
        return gateway

    gh = get_github_client()
    analyzer = get_analyzer()
    sandbox = get_sandbox_manager()
    memory = get_memory_tool()

    # GitHub tools (SPEC 8.1)
    gateway.register("github.get_pr", gh.get_pr)
    gateway.register("github.get_changed_files", gh.get_changed_files)
    gateway.register("github.get_diff", gh.get_diff)
    gateway.register("github.get_file_content", gh.get_file_content)
    gateway.register("github.create_review", gh.create_review)
    gateway.register("github.create_comment", gh.create_comment)
    gateway.register("github.create_check_run", gh.create_check_run)

    # Sandbox tools (SPEC 8.2)
    gateway.register("sandbox.create", sandbox.create)
    gateway.register("sandbox.prepare_workspace", sandbox.prepare_workspace)
    gateway.register("sandbox.install_dependencies", sandbox.install_dependencies)
    gateway.register("sandbox.exec", sandbox.exec)
    gateway.register("sandbox.apply_patch", sandbox.apply_patch)
    gateway.register("sandbox.get_diff", sandbox.get_diff)
    gateway.register("sandbox.list_files", sandbox.list_files)
    gateway.register("sandbox.read_files", sandbox.read_files)
    gateway.register("sandbox.write_temp_files", sandbox.write_temp_files)
    gateway.register("sandbox.reset_temp_files", sandbox.reset_temp_files)
    gateway.register("sandbox.destroy", sandbox.destroy)
    gateway.register("sandbox.destroy_all_for_review", sandbox.destroy_all_for_review)

    # Analyzer tools (SPEC 8.3)
    gateway.register("analyzer.detect_language", analyzer.detect_language)
    gateway.register("analyzer.detect_test_commands", analyzer.detect_test_commands)
    gateway.register("analyzer.lint", analyzer.lint)
    gateway.register("analyzer.security_scan", analyzer.security_scan)

    # Memory tools (SPEC 8.4)
    gateway.register("memory.query", memory.query)
    gateway.register("memory.store", memory.store)
    gateway.register("memory.delete", memory.delete)

    _gateway_registered = True
    logger.info("tools.registered")
    return gateway


def reset_all_tools() -> None:
    """重置所有工具单例（测试用）。"""
    global _gateway_registered
    _gateway_registered = False
    reset_gateway()
    reset_github_client()
    reset_sandbox_manager()


__all__ = [
    "ToolGateway",
    "get_gateway",
    "register_tools",
    "reset_all_tools",
    "GitHubClient",
    "GitHubAPIError",
    "GitHubAuthError",
    "get_github_client",
    "SandboxManager",
    "DockerSandboxManager",
    "LocalSandboxManager",
    "get_sandbox_manager",
    "ExecResult",
    "AnalyzerTool",
    "get_analyzer",
    "MemoryTool",
    "get_memory_tool",
]
