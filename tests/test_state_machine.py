"""状态机测试（SPEC 第 4 节）。"""

from __future__ import annotations

import pytest

from app.models import ReviewState, ReviewTask
from app.orchestrator.state_machine import StateMachine, StateMachineError


def _make_task(status=ReviewState.RECEIVED) -> ReviewTask:
    return ReviewTask(
        review_id="r1", repository_id=1, repository_full_name="o/r",
        pr_number=1, base_sha="b", head_sha="h", status=status,
    )


class TestStateMachine:
    def test_valid_transition(self):
        assert StateMachine.can_transition(ReviewState.RECEIVED, ReviewState.VALIDATING_EVENT)
        assert StateMachine.can_transition(ReviewState.PLANNING, ReviewState.SANDBOX_PREPARE)
        assert StateMachine.can_transition(ReviewState.STATIC_REVIEW, ReviewState.DYNAMIC_TESTING)

    def test_invalid_transition(self):
        assert not StateMachine.can_transition(ReviewState.RECEIVED, ReviewState.COMPLETED)
        assert not StateMachine.can_transition(ReviewState.RECEIVED, ReviewState.STATIC_REVIEW)

    def test_terminal_no_transition(self):
        for terminal in [ReviewState.COMPLETED, ReviewState.FAILED, ReviewState.CANCELLED,
                         ReviewState.SUPERSEDED, ReviewState.SKIPPED]:
            assert not StateMachine.can_transition(terminal, ReviewState.PLANNING)

    def test_transition_updates_task(self):
        task = _make_task()
        StateMachine.transition(task, ReviewState.VALIDATING_EVENT)
        assert task.status == ReviewState.VALIDATING_EVENT

    def test_invalid_transition_raises(self):
        task = _make_task()
        with pytest.raises(StateMachineError):
            StateMachine.transition(task, ReviewState.COMPLETED)

    def test_force_cancel_from_any_state(self):
        task = _make_task(status=ReviewState.STATIC_REVIEW)
        StateMachine.transition(task, ReviewState.CANCELLED, force=True)
        assert task.status == ReviewState.CANCELLED
        assert task.completed_at is not None

    def test_force_supersede(self):
        task = _make_task(status=ReviewState.DYNAMIC_TESTING)
        StateMachine.transition(task, ReviewState.SUPERSEDED, force=True)
        assert task.status == ReviewState.SUPERSEDED

    def test_normal_pipeline_transitions(self):
        """完整正常流转路径。"""
        task = _make_task()
        for target in [
            ReviewState.VALIDATING_EVENT,
            ReviewState.PREPROCESSING,
            ReviewState.PLANNING,
            ReviewState.SANDBOX_PREPARE,
            ReviewState.STATIC_REVIEW,
            ReviewState.DYNAMIC_TESTING,
            ReviewState.ISSUE_AGGREGATION,
            ReviewState.PATCH_GENERATION,
            ReviewState.PATCH_VALIDATION,
            ReviewState.REVIEW_PUBLISHING,
            ReviewState.COMPLETED,
        ]:
            StateMachine.transition(task, target)
        assert task.status == ReviewState.COMPLETED

    def test_skip_path(self):
        """文档变更跳过路径。"""
        task = _make_task()
        StateMachine.transition(task, ReviewState.VALIDATING_EVENT)
        StateMachine.transition(task, ReviewState.SKIPPED)
        assert task.status == ReviewState.SKIPPED
        assert StateMachine.is_terminal(ReviewState.SKIPPED)

    def test_next_normal_states(self):
        states = StateMachine.next_normal_states(ReviewState.PLANNING)
        assert ReviewState.SANDBOX_PREPARE in states
        assert ReviewState.FAILED not in states
