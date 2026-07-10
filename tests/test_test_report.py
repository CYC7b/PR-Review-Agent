"""Tests for test-engineer result semantics and safe sandbox artefacts."""

from app.agents.review_publisher import ReviewPublisher
from app.agents.test_executor import TestExecutor
from app.models import TestCommandResult, TestRunReport, TestRunStatus
from app.tools.sandbox_tool import _safe_relative_path


def test_incomplete_test_report_makes_check_neutral():
    report = TestRunReport(status=TestRunStatus.INCOMPLETE, limitations=["dependencies unavailable"])
    assert ReviewPublisher._determine_conclusion([], [], {"report": report}) == "neutral"


def test_temp_test_paths_cannot_escape_workspace():
    assert _safe_relative_path(".pr-review-agent-tests/test_case.py", temp_only=True).endswith("test_case.py")
    try:
        _safe_relative_path("../outside.py", temp_only=True)
    except ValueError:
        pass
    else:
        raise AssertionError("path traversal was accepted")


def test_base_pass_confirms_head_test_failure_as_pr_regression():
    executor = TestExecutor(review_id="r", head_sha="head", sandbox_id="s")
    issue = executor._failure_to_issue(
        "python -m pytest -q", {"exit_code": 1, "stdout": "FAILED src/app.py", "stderr": ""},
        [{"path": "src/app.py"}],
    )
    assert issue is not None
    result = {
        "report": TestRunReport(
            status=TestRunStatus.FAILED,
            commands=[TestCommandResult(command="python -m pytest -q", exit_code=1)],
        ),
        "issues": [issue],
    }
    executor.reconcile_baseline(result, {"python -m pytest -q": 0})
    assert issue.introduced_by_pr is True
    assert result["report"].confirmed_regressions == ["python -m pytest -q"]
