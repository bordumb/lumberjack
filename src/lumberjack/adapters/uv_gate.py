"""The gate: ruff, then ty, then pytest, fail-fast between stages.

One timeout is a slow machine and worth another go; the same check timing out twice
running is a hung suite, and re-running it only spends another fifteen minutes finding
that out again.  :class:`CommandGate` counts consecutive timeouts per check per worktree
so the two are told apart, in the report the agent reads and in the log.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import timedelta

from lumberjack.domain.gate import CheckOutcome, CheckResult, GateReport
from lumberjack.domain.workstream import Worktree

__all__ = ["CommandGate", "NullGate"]

log = logging.getLogger(__name__)

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
    timeouts: dict[tuple[str, str], int] = field(default_factory=dict)
    """Consecutive timeouts per (worktree, check).  Mutated, never rebound, so the
    dataclass stays frozen and two gates never share a count."""

    def hung(self, worktree: Worktree) -> tuple[str, ...]:
        """Checks that have now timed out at least twice running in this worktree."""
        return tuple(
            name
            for (path, name), count in sorted(self.timeouts.items())
            if path == str(worktree.path) and count >= 2
        )

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
            log.warning("gate could not start %s in %s: %s", command, worktree.path, error)
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
            return self._timed_out(command, worktree)
        self.timeouts.pop((str(worktree.path), _name(command)), None)
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


    def _timed_out(self, command: tuple[str, ...], worktree: Worktree) -> CheckResult:
        """A timeout, told apart from *the same* timeout happening again.

        Once, the honest advice is "try again, and consider whether the machine is
        loaded". Twice running, the honest advice is "your suite hangs" -- and repeating
        a fifteen-minute wait to rediscover that is the opposite of useful.
        """
        key = (str(worktree.path), _name(command))
        count = self.timeouts.get(key, 0) + 1
        self.timeouts[key] = count
        if count >= 2:
            log.error(
                "%s timed out in %s %d times running: treating it as a hung suite",
                _name(command),
                worktree.path,
                count,
            )
            detail = (
                f"{_name(command)} timed out after {self.timeout}, {count} times running. "
                "This is a hung suite, not a slow machine: find the test that never "
                "returns rather than running the gate again."
            )
        else:
            log.warning("%s timed out in %s after %s", _name(command), worktree.path, self.timeout)
            detail = (
                f"{_name(command)} timed out after {self.timeout}. That can be a loaded "
                "machine; if it happens again it is a hung suite."
            )
        return CheckResult(
            name=_name(command),
            command=command,
            outcome=CheckOutcome.ERRORED,
            log_excerpt=detail,
            duration=self.timeout,
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
