"""配置加载：合并 default.yaml → config.yaml → 环境变量（最小权限优先）。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"


class ReviewConfig(BaseModel):
    trigger_events: list[str] = Field(default_factory=lambda: [
        "pull_request.opened",
        "pull_request.synchronize",
        "pull_request.reopened",
    ])
    max_changed_files: int = 100
    max_diff_lines: int = 5000
    skip_docs_only: bool = True
    enable_auto_patch: bool = False
    publish_low_confidence_findings: bool = False


class AgentsConfig(BaseModel):
    max_parallel_agents: int = 4
    planner_timeout_seconds: int = 120
    agent_timeout_seconds: int = 600


class SandboxConfig(BaseModel):
    enabled: bool = True
    default_timeout_seconds: int = 1800
    cpu_limit: int = 2
    memory_limit: str = "4Gi"
    disk_limit: str = "10Gi"
    pid_limit: int = 200
    network_default: str = "disabled"
    dependency_network_allowlist: list[str] = Field(default_factory=lambda: [
        "pypi.org",
        "files.pythonhosted.org",
        "registry.npmjs.org",
        "proxy.golang.org",
    ])
    image: str = "pr-review-sandbox:latest"
    workspace: str = "/workspace"
    # 容器生命周期上限（秒），超过后强制回收（SPEC 9.2）
    max_lifetime_seconds: int = 1800
    # 依赖解析阶段的受限出网代理。为空则依赖阶段完全无网络（fail-closed），
    # 绝不放开无限制公网出网（SPEC 9.3 C3）。
    egress_proxy_url: str = ""
    # 是否允许在 Docker 不可用时回退到本地（宿主机）执行。
    # 本地执行不提供隔离，仅限开发；生产必须为 False（SPEC 9 / C1）。
    allow_insecure_local_fallback: bool = False


class PatchConfig(BaseModel):
    max_retry: int = 2
    min_confidence_for_patch: float = 0.8
    allow_new_dependencies: bool = False
    allow_public_api_changes: bool = False
    publish_only_if_validated: bool = True


class TestingConfig(BaseModel):
    """Bounded test-engineer behaviour for one PR review."""

    enabled: bool = True
    budget_seconds: int = 900
    max_generated_scenarios: int = 6
    max_test_repair_attempts: int = 2
    compare_base_on_failure: bool = True
    allow_local_integration: bool = True


class MemoryConfig(BaseModel):
    enable_repo_memory: bool = False
    enable_org_memory: bool = False
    store_full_code: bool = False
    retention_days: int = 90


class ApiConfig(BaseModel):
    # 是否强制要求管理类端点（/api/v1/reviews*）携带 Bearer token。
    # 为 True 时若未配置 API_KEY 则拒绝请求（fail-closed，与 webhook 签名策略一致）。
    require_api_key: bool = True


class GitHubConfig(BaseModel):
    use_pending_review: bool = True
    deduplicate_comments: bool = True
    bind_to_head_sha: bool = True
    # 是否强制校验 webhook 签名。为 True 时若未配置 secret 则拒绝处理事件
    # （fail-closed，SPEC 5.1 / C5）。仅开发环境可显式关闭。
    require_webhook_signature: bool = True
    # GitHub API 触发限流时的最大重试次数（SPEC 15.1 / H4）。
    api_max_retries: int = 3


class AppConfig(BaseModel):
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    patch: PatchConfig = Field(default_factory=PatchConfig)
    testing: TestingConfig = Field(default_factory=TestingConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    github: GitHubConfig = Field(default_factory=GitHubConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)


class EnvSettings(BaseSettings):
    """环境变量层配置（凭据、运行时参数）。"""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_log_level: str = "INFO"

    database_url: str = "sqlite:///./data/pr_review.db"
    redis_url: str = ""

    # API 鉴权（管理类端点，如 /api/v1/reviews*）
    api_key: str = ""

    # GitHub App
    github_app_id: str = ""
    github_private_key_path: str = ""
    github_private_key: str = ""
    github_webhook_secret: str = ""
    github_token: str = ""

    # LLM
    llm_api_base: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: int = 120
    llm_mode: str = "openai"  # openai | mock
    # 单次补全的最大 token 数。推理（thinking）模型会先消耗 token 思考，
    # 需为思考+正文预留充足预算，否则正文可能被截断为空。默认 8192。
    llm_max_tokens: int = 8192

    # Sandbox
    sandbox_enabled: bool = True


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并 override 到 base。"""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data or {}


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    """加载并合并 YAML 配置。"""
    default_data = _load_yaml(CONFIG_DIR / "default.yaml")
    user_data = _load_yaml(CONFIG_DIR / "config.yaml")
    merged = _deep_merge(default_data, user_data)
    return AppConfig(**merged)


@lru_cache(maxsize=1)
def get_settings() -> EnvSettings:
    return EnvSettings()


def reload_config() -> tuple[AppConfig, EnvSettings]:
    """清除缓存并重新加载（测试用）。"""
    get_config.cache_clear()
    get_settings.cache_clear()
    return get_config(), get_settings()


def ensure_data_dir() -> Path:
    """确保数据目录存在（SQLite/日志）。"""
    data_dir = PROJECT_ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir
