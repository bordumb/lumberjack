"""The quality bar a workstream must clear to land."""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from lumberjack.ids import ArtifactRef

__all__ = ["CheckOutcome", "CheckResult", "GateReport"]


class CheckOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERRORED = "errored"


class CheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    command: tuple[str, ...]
    outcome: CheckOutcome
    exit_code: int | None = None
    duration: timedelta = timedelta(0)
    log_excerpt: str = Field(default="", max_length=8000)
    log_ref: ArtifactRef | None = None

    @property
    def passed(self) -> bool:
        return self.outcome in (CheckOutcome.PASSED, CheckOutcome.SKIPPED)


class GateReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    checks: tuple[CheckResult, ...] = ()
    duration: timedelta = timedelta(0)

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def first_failure(self) -> CheckResult | None:
        return next((check for check in self.checks if not check.passed), None)

    def render(self, limit: int = 2000) -> str:
        failure = self.first_failure
        if failure is None:
            names = ", ".join(check.name for check in self.checks)
            return f"gate passed ({names or 'no checks'})"
        head = f"gate failed at {failure.name} (exit {failure.exit_code})"
        return f"{head}\n{failure.log_excerpt[:limit]}"
