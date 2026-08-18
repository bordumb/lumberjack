"""The gate: ruff, then ty, then pytest, fail-fast between stages."""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from datetime import timedelta

from lumberjack.domain.gate import CheckOutcome, CheckResult, GateReport
from lumberjack.domain.workstream import Worktree

__all__ = ["CommandGate", "NullGate"]

DEFAULT_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("uv", "run", "ruff", "check", "."),
    ("uv", "run", "ty", "check"),
    ("uv", "run", "pytest", "-q"),
)


@dataclass(frozen=True, slots=True)
class CommandGate:
    """Runs a fixed sequence of shell checks inside a worktree."""

    commands: tuple[tuple[str, ...], ...] = DEFAULT_COMMANDS
    timeout: timedelta = timedelta(minutes=15)
    fail_fast: bool = True
    excerpt_limit: int = 8000
    env: dict[str, str] = field(default_factory=dict)

    async def run(self, worktree: Worktree) -> GateReport:
        started = time.monotonic()
        results: list[CheckResult] = []
        for command in self.commands:
            result = await self._run_one(command, worktree)
            results.append(result)
            if self.fail_fast and not result.passed:
                results.extend(
                    CheckResult(name=_name(rest), command=rest, outcome=CheckOutcome.SKIPPED)
                    for rest in self.commands[len(results) :]
                )
                break
        return GateReport(
            checks=tuple(results),
            duration=timedelta(seconds=time.monotonic() - started),
        )

    async def _run_one(self, command: tuple[str, ...], worktree: Worktree) -> CheckResult:
        started = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(worktree.path),
                env={**os.environ, **self.env},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except (OSError, ValueError) as error:
            return CheckResult(
                name=_name(command),
                command=command,
                outcome=CheckOutcome.ERRORED,
                log_excerpt=str(error),
                duration=timedelta(seconds=time.monotonic() - started),
            )
        try:
            raw, _ = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout.total_seconds()
            )
        except TimeoutError:
            process.kill()
            return CheckResult(
                name=_name(command),
                command=command,
                outcome=CheckOutcome.ERRORED,
                log_excerpt=f"timed out after {self.timeout}",
                duration=self.timeout,
            )
        code = process.returncode or 0
        output = raw.decode("utf-8", "replace")
        return CheckResult(
            name=_name(command),
            command=command,
            outcome=CheckOutcome.PASSED if code == 0 else CheckOutcome.FAILED,
            exit_code=code,
            log_excerpt=output[-self.excerpt_limit :],
            duration=timedelta(seconds=time.monotonic() - started),
        )


def _name(command: tuple[str, ...]) -> str:
    meaningful = [part for part in command if part not in ("uv", "run", "-q", "--")]
    return meaningful[0] if meaningful else command[0]


@dataclass(frozen=True, slots=True)
class NullGate:
    """Always passes.  For dry runs and tests that are not about the gate."""

    async def run(self, worktree: Worktree) -> GateReport:
        _ = worktree
        return GateReport(checks=())
