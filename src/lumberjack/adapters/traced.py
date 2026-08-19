"""Tracing wrappers for the git and gate boundaries.

Wrapping beats sprinkling: every git call the harness makes goes through one object, so
instrumenting the port costs one module instead of a timer at forty call sites, and the
core stays free of measurement code.  :mod:`lumberjack.adapters.projecting` is the same
shape -- a dataclass holding ``inner`` that delegates every method.

The two numbers worth the wrapper are ``merge_tree``, which is the oracle's unit cost
and therefore what settles "optimal N", and ``snapshot``, which is what says whether the
sensor debounce is set anywhere near sensibly.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from lumberjack.ports.gate import Gate
from lumberjack.ports.git import GitBackend
from lumberjack.ports.telemetry import AttrValue, NullTelemetry, Span, Telemetry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from lumberjack.domain.gate import GateReport
    from lumberjack.domain.vcs import (
        FileChange,
        MergeOutcome,
        MergeTreeResult,
        RebaseOutcome,
        WorkingStatus,
    )
    from lumberjack.domain.workstream import Snapshot, Worktree
    from lumberjack.ids import CommitSha, RepoPath

__all__ = ["TracedGate", "TracedGit", "instrumented"]


def instrumented(
    *, git: GitBackend, gate: Gate, telemetry: Telemetry
) -> tuple[GitBackend, Gate]:
    """Wrap the two ports worth timing.

    One call, so the three composition roots -- ``Stand``, ``lj`` and the MCP server --
    cannot drift into instrumenting different things.  The wrappers go on unconditionally:
    against :class:`NullTelemetry` they are a no-op context manager, and a stand that has
    to be reconfigured before it can be measured is one nobody measures.
    """
    return TracedGit(inner=git, telemetry=telemetry), TracedGate(inner=gate, telemetry=telemetry)


@dataclass(slots=True)
class TracedGit:
    """Every :class:`~lumberjack.ports.git.GitBackend` call, timed and spanned.

    Each operation emits a ``lj.git.<operation>`` span and a histogram of the same name
    in milliseconds.  Failures are recorded on the span and re-raised: this measures
    git, it does not decide anything about it.
    """

    inner: GitBackend
    telemetry: Telemetry = field(default_factory=NullTelemetry)

    @asynccontextmanager
    async def _timed(self, operation: str, **attributes: AttrValue) -> AsyncIterator[Span]:
        started = time.perf_counter()
        with self.telemetry.span(f"lj.git.{operation}", **attributes) as span:
            try:
                yield span
            except BaseException as error:
                span.record_error(error)
                self.telemetry.histogram(
                    f"lj.git.{operation}",
                    (time.perf_counter() - started) * 1000,
                    failed=True,
                    **attributes,
                )
                raise
            self.telemetry.histogram(
                f"lj.git.{operation}",
                (time.perf_counter() - started) * 1000,
                failed=False,
                **attributes,
            )

    # -- refs and branches -----------------------------------------------------------

    async def resolve(self, ref: str) -> CommitSha:
        async with self._timed("resolve"):
            return await self.inner.resolve(ref)

    async def create_branch(self, name: str, at: CommitSha, *, force: bool = False) -> None:
        async with self._timed("create_branch"):
            await self.inner.create_branch(name, at, force=force)

    async def delete_branch(self, name: str) -> None:
        async with self._timed("delete_branch"):
            await self.inner.delete_branch(name)

    # -- worktrees -------------------------------------------------------------------

    async def add_worktree(self, branch: str, base: CommitSha, at: Path) -> Worktree:
        async with self._timed("add_worktree", branch=branch):
            return await self.inner.add_worktree(branch, base, at)

    async def attach_worktree(self, branch: str, at: Path) -> Worktree:
        async with self._timed("attach_worktree", branch=branch):
            return await self.inner.attach_worktree(branch, at)

    async def remove_worktree(self, worktree: Worktree, *, force: bool = False) -> None:
        async with self._timed("remove_worktree", branch=worktree.branch):
            await self.inner.remove_worktree(worktree, force=force)

    async def list_worktrees(self) -> tuple[Worktree, ...]:
        async with self._timed("list_worktrees"):
            return await self.inner.list_worktrees()

    async def status(self, worktree: Worktree) -> WorkingStatus:
        async with self._timed("status", branch=worktree.branch):
            return await self.inner.status(worktree)

    async def snapshot(self, worktree: Worktree, *, message: str = "lj-snapshot") -> Snapshot:
        """The sensor's cost: how often a worktree can be captured is a debounce setting."""
        async with self._timed("snapshot", branch=worktree.branch) as span:
            snapshot = await self.inner.snapshot(worktree, message=message)
            span.set(dirty=snapshot.dirty, paths=len(snapshot.paths))
            return snapshot

    async def head(self, worktree: Worktree) -> CommitSha:
        async with self._timed("head", branch=worktree.branch):
            return await self.inner.head(worktree)

    # -- merging ---------------------------------------------------------------------

    async def merge_base(self, left: CommitSha, right: CommitSha) -> CommitSha | None:
        async with self._timed("merge_base"):
            return await self.inner.merge_base(left, right)

    async def merge_tree(
        self, ours: CommitSha, theirs: CommitSha, *, base: CommitSha | None = None
    ) -> MergeTreeResult:
        """The oracle's unit cost.  0001_SPEC.md §18.2 asks for "optimal N"; this answers it."""
        async with self._timed("merge_tree") as span:
            result = await self.inner.merge_tree(ours, theirs, base=base)
            span.set(clean=result.clean, conflicted=len(result.conflicted))
            return result

    async def merge(self, branch: str, into: str, *, message: str) -> MergeOutcome:
        async with self._timed("merge", branch=branch, into=into) as span:
            outcome = await self.inner.merge(branch, into, message=message)
            span.set(ok=outcome.ok, status=outcome.status)
            return outcome

    async def rebase(self, worktree: Worktree, onto: CommitSha) -> RebaseOutcome:
        async with self._timed("rebase", branch=worktree.branch) as span:
            outcome = await self.inner.rebase(worktree, onto)
            span.set(ok=outcome.ok, status=outcome.status)
            return outcome

    # -- reading ---------------------------------------------------------------------

    async def changes(self, base: CommitSha, tip: CommitSha) -> tuple[FileChange, ...]:
        async with self._timed("changes"):
            return await self.inner.changes(base, tip)

    async def commits_between(self, base: CommitSha, tip: CommitSha) -> int:
        async with self._timed("commits_between"):
            return await self.inner.commits_between(base, tip)

    async def read_blob(self, commit: CommitSha, path: RepoPath) -> bytes | None:
        async with self._timed("read_blob"):
            return await self.inner.read_blob(commit, path)

    async def list_files(self, commit: CommitSha) -> tuple[RepoPath, ...]:
        async with self._timed("list_files"):
            return await self.inner.list_files(commit)

    async def churn(self, since: str = "200") -> dict[RepoPath, int]:
        async with self._timed("churn"):
            return await self.inner.churn(since)

    async def commit_all(self, worktree: Worktree, message: str) -> CommitSha | None:
        async with self._timed("commit_all", branch=worktree.branch):
            return await self.inner.commit_all(worktree, message)


@dataclass(slots=True)
class TracedGate:
    """``lj.gate.run``: whether the quality bar, not the agents, is the bottleneck."""

    inner: Gate
    telemetry: Telemetry = field(default_factory=NullTelemetry)

    async def run(self, worktree: Worktree) -> GateReport:
        started = time.perf_counter()
        with self.telemetry.span("lj.gate.run", branch=worktree.branch) as span:
            report = await self.inner.run(worktree)
            failure = report.first_failure
            first_failure = failure.name if failure is not None else ""
            span.set(passed=report.passed, first_failure=first_failure)
            self.telemetry.histogram(
                "lj.gate.run",
                (time.perf_counter() - started) * 1000,
                passed=report.passed,
                first_failure=first_failure,
            )
            return report
