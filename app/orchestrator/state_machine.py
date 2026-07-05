"""审查状态机（SPEC 第 4 节）。

定义合法状态流转、幂等规则与取消/取代逻辑。
"""

from __future__ import annotations

from app.models import TERMINAL_STATES, ReviewState, ReviewTask

# 合法状态流转图（SPEC 4.1）
VALID_TRANSITIONS: dict[ReviewState, set[ReviewState]] = {
    ReviewState.RECEIVED: {
        ReviewState.VALIDATING_EVENT,
        ReviewState.SKIPPED,
        ReviewState.FAILED,
        ReviewState.CANCELLED,
    },
    ReviewState.VALIDATING_EVENT: {
        ReviewState.PREPROCESSING,
        ReviewState.SKIPPED,
        ReviewState.FAILED,
        ReviewState.CANCELLED,
        ReviewState.SUPERSEDED,
    },
    ReviewState.PREPROCESSING: {
        ReviewState.PLANNING,
        ReviewState.SKIPPED,
        ReviewState.FAILED,
        ReviewState.CANCELLED,
        ReviewState.SUPERSEDED,
        ReviewState.TIMEOUT,
    },
    ReviewState.PLANNING: {
        ReviewState.SANDBOX_PREPARE,
        ReviewState.STATIC_REVIEW,
        ReviewState.ISSUE_AGGREGATION,
        ReviewState.SKIPPED,
        ReviewState.FAILED,
        ReviewState.CANCELLED,
        ReviewState.SUPERSEDED,
        ReviewState.TIMEOUT,
    },
    ReviewState.SANDBOX_PREPARE: {
        ReviewState.STATIC_REVIEW,
        ReviewState.FAILED,
        ReviewState.CANCELLED,
        ReviewState.SUPERSEDED,
        ReviewState.TIMEOUT,
    },
    ReviewState.STATIC_REVIEW: {
        ReviewState.DYNAMIC_TESTING,
        ReviewState.ISSUE_AGGREGATION,
        ReviewState.FAILED,
        ReviewState.CANCELLED,
        ReviewState.SUPERSEDED,
        ReviewState.TIMEOUT,
    },
    ReviewState.DYNAMIC_TESTING: {
        ReviewState.ISSUE_AGGREGATION,
        ReviewState.REVIEW_PUBLISHING,
        ReviewState.FAILED,
        ReviewState.CANCELLED,
        ReviewState.SUPERSEDED,
        ReviewState.TIMEOUT,
    },
    ReviewState.ISSUE_AGGREGATION: {
        ReviewState.PATCH_GENERATION,
        ReviewState.REVIEW_PUBLISHING,
        ReviewState.FAILED,
        ReviewState.CANCELLED,
        ReviewState.SUPERSEDED,
        ReviewState.TIMEOUT,
    },
    ReviewState.PATCH_GENERATION: {
        ReviewState.PATCH_VALIDATION,
        ReviewState.REVIEW_PUBLISHING,
        ReviewState.FAILED,
        ReviewState.CANCELLED,
        ReviewState.SUPERSEDED,
        ReviewState.TIMEOUT,
    },
    ReviewState.PATCH_VALIDATION: {
        ReviewState.PATCH_GENERATION,  # 重试
        ReviewState.REVIEW_PUBLISHING,
        ReviewState.FAILED,
        ReviewState.CANCELLED,
        ReviewState.SUPERSEDED,
        ReviewState.TIMEOUT,
    },
    ReviewState.REVIEW_PUBLISHING: {
        ReviewState.COMPLETED,
        ReviewState.FAILED,
        ReviewState.CANCELLED,
        ReviewState.SUPERSEDED,
        ReviewState.TIMEOUT,
    },
    ReviewState.COMPLETED: set(),
    ReviewState.FAILED: set(),
    ReviewState.CANCELLED: set(),
    ReviewState.SUPERSEDED: set(),
    ReviewState.TIMEOUT: set(),
    ReviewState.SKIPPED: set(),
}


class StateMachineError(Exception):
    """非法状态流转。"""


class StateMachine:
    """审查任务状态机。"""

    @staticmethod
    def can_transition(current: ReviewState, target: ReviewState) -> bool:
        if current in TERMINAL_STATES:
            return False
        return target in VALID_TRANSITIONS.get(current, set())

    @staticmethod
    def transition(task: ReviewTask, target: ReviewState, *, force: bool = False) -> ReviewTask:
        if not StateMachine.can_transition(task.status, target):
            if force:
                # 允许从任意非终态直接进入终态（用于强制取消）
                if target in TERMINAL_STATES:
                    task.transition(target)
                    return task
            raise StateMachineError(
                f"非法状态流转: {task.status.value} → {target.value}"
            )
        task.transition(target)
        return task

    @staticmethod
    def is_terminal(state: ReviewState) -> bool:
        return state in TERMINAL_STATES

    @staticmethod
    def next_normal_states(current: ReviewState) -> set[ReviewState]:
        """返回正常的（非异常）后续状态。"""
        all_targets = VALID_TRANSITIONS.get(current, set())
        return {s for s in all_targets if s not in {ReviewState.FAILED, ReviewState.CANCELLED,
                                                      ReviewState.SUPERSEDED, ReviewState.TIMEOUT}}
