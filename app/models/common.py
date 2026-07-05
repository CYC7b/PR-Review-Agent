"""公共枚举与基础类型（对应 SPEC 第 4、6 节）。"""

from __future__ import annotations

from enum import Enum


class ReviewState(str, Enum):
    """审查状态机状态（SPEC 4.1）。"""

    RECEIVED = "RECEIVED"
    VALIDATING_EVENT = "VALIDATING_EVENT"
    PREPROCESSING = "PREPROCESSING"
    PLANNING = "PLANNING"
    SANDBOX_PREPARE = "SANDBOX_PREPARE"
    STATIC_REVIEW = "STATIC_REVIEW"
    DYNAMIC_TESTING = "DYNAMIC_TESTING"
    ISSUE_AGGREGATION = "ISSUE_AGGREGATION"
    PATCH_GENERATION = "PATCH_GENERATION"
    PATCH_VALIDATION = "PATCH_VALIDATION"
    REVIEW_PUBLISHING = "REVIEW_PUBLISHING"
    COMPLETED = "COMPLETED"

    # 异常状态
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"
    TIMEOUT = "TIMEOUT"
    SKIPPED = "SKIPPED"


# 终态集合：到达后不再流转
TERMINAL_STATES = frozenset({
    ReviewState.COMPLETED,
    ReviewState.FAILED,
    ReviewState.CANCELLED,
    ReviewState.SUPERSEDED,
    ReviewState.TIMEOUT,
    ReviewState.SKIPPED,
})


class IssueType(str, Enum):
    SECURITY = "security"
    BUG = "bug"
    PERFORMANCE = "performance"
    MAINTAINABILITY = "maintainability"
    TEST_FAILURE = "test_failure"
    STYLE = "style"


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class VerificationStatus(str, Enum):
    NOT_RUN = "not_run"
    PASSED = "passed"
    FAILED = "failed"
    PARTIAL = "partial"
    SKIPPED = "skipped"
    TIMEOUT = "timeout"


class PublicationStatus(str, Enum):
    PENDING = "pending"
    PUBLISHED = "published"
    SUPPRESSED = "suppressed"


class PatchPublishMode(str, Enum):
    GITHUB_SUGGESTION = "github_suggestion"
    PATCH_COMMENT = "patch_comment"
    BOT_BRANCH = "bot_branch"
    SUPPRESSED = "suppressed"


class ChangeType(str, Enum):
    ADDED = "added"
    MODIFIED = "modified"
    REMOVED = "removed"
    RENAMED = "renamed"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class PrChangeCategory(str, Enum):
    DOCS = "docs"
    TEST = "test"
    CODE = "code"
    CONFIG = "config"
    DEPENDENCY = "dependency"
    SECURITY_SENSITIVE = "security_sensitive"


class MemoryScope(str, Enum):
    TASK = "task"
    REPO = "repo"
    ORG = "org"
    GLOBAL = "global"
