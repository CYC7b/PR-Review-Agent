"""Structured evidence emitted by the sandbox test-engineer workflow."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class TestRunStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    INCOMPLETE = "incomplete"
    SKIPPED = "skipped"
    TIMEOUT = "timeout"
    FLAKY = "flaky"


class TestCommandResult(BaseModel):
    command: str
    phase: str = "existing"
    exit_code: int | None = None
    timed_out: bool = False
    duration_ms: int = 0
    stdout: str = ""
    stderr: str = ""
    base_exit_code: int | None = None
    regression: bool = False


class TestScenario(BaseModel):
    name: str
    rationale: str = ""
    target_files: list[str] = Field(default_factory=list)
    test_file: str = ""
    command: str = ""
    status: TestRunStatus = TestRunStatus.SKIPPED
    attempts: int = 0
    limitation: str = ""


class TestRunReport(BaseModel):
    """Persisted test evidence; an empty command list is never a pass."""

    status: TestRunStatus = TestRunStatus.SKIPPED
    complete: bool = False
    languages: list[str] = Field(default_factory=list)
    framework: str = ""
    commands: list[TestCommandResult] = Field(default_factory=list)
    scenarios: list[TestScenario] = Field(default_factory=list)
    confirmed_regressions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    dependency_ready: bool = False
    duration_ms: int = 0
