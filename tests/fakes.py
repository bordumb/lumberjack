"""Reusable test doubles.

Fault injection was the capability the suite was missing.  The ports exist precisely so
that failures can be injected at them, and every wrapper here has the same shape as
``adapters/projecting.py``: a dataclass holding ``inner`` that delegates, with a queue of
faults per method name.  They are wrappers rather than one-off mocks on purpose -- a
:class:`FaultyGit` in front of the *real* ``GitCli`` still does real git, which is what
makes "fails once, then succeeds" a meaningful test rather than a tautology.

::

    git = FaultyGit(inner=GitCli(repo=repo), faults={"snapshot": [index_locked()]})
    # the first snapshot raises; the second is a real snapshot of a real worktree
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from lumberjack.agents.outputs import WorkerOutput
from lumberjack.core.services import Services
from lumberjack.domain.gate import GateReport
from lumberjack.domain.task import TaskSpec
from lumberjack.domain.vcs import (
    FileChange,
    MergeOutcome,
    MergeTreeResult,
    RebaseOutcome,
    WorkingStatus,
)
from lumberjack.domain.workstream import Snapshot, Workstream, Worktree
from lumberjack.ids import CommitSha, RepoPath, WorkstreamId
from lumberjack.ports.gate import Gate
from lumberjack.ports.git import GitBackend, GitError
from lumberjack.ports.runner import WorkerRunner

__all__ = [
    "FaultyGate",
    "FaultyGit",
    "FaultyRunner",
    "StubUsage",
    "StubUsageTotals",
    "always",
    "index_locked",
    "not_a_repo",
    "rewired",
]


def rewired(services: Services, **config_updates: Any) -> Services:
    """The same stand with a different :class:`StandConfig`.

    ``Services.wire`` hands the config to the broker, the oracle and the train at
    construction, so changing one field means rebuilding the bundle -- not patching the
    dataclass and hoping.  The ledger and projections are shared, so assertions against
    the original fixture still see everything that happens.
    """
    return Services.wire(
        stand=services.stand,
        config=services.config.model_copy(update=config_updates),
        clock=services.clock,
        git=services.git,
        ledger=services.ledger,
        indexer=services.indexer,
        gate=services.gate,
        projections=services.projections,
    )


def index_locked(command: str = "snapshot") -> GitError:
    """The transient failure the retry allowlist exists for."""
    return GitError(
        (command,),
        128,
        "fatal: Unable to create '/repo/.git/index.lock': File exists.",
    )


def not_a_repo(command: str = "rev-parse") -> GitError:
    """A terminal git failure: retrying it only wastes time."""
    return GitError((command,), 128, "fatal: not a git repository")


def always(error: BaseException, times: int = 1000) -> list[BaseException | None]:
    """A fault queue long enough to outlast any loop under test."""
    return [error] * times


@dataclass
class FaultyGit:
    """A :class:`GitBackend` that fails on demand and otherwise delegates.

    ``faults`` maps a method name to a queue consumed one call at a time; ``None`` in a
    queue means "let this call through".  Anything not named behaves normally, so the
    success path in a test is the real adapter doing real work.

    Written out method by method rather than through ``__getattr__`` for the same reason
    ``adapters/projecting.py`` is: the delegation is the interface, and a wrapper the
    type checker cannot see through is a wrapper that silently stops matching the port.
    """

    inner: GitBackend
    faults: dict[str, list[BaseException | None]] = field(default_factory=dict)
    calls: Counter[str] = field(default_factory=Counter)

    def _check(self, name: str) -> None:
        self.calls[name] += 1
        queue = self.faults.get(name)
        if queue:
            error = queue.pop(0)
            if error is not None:
                raise error

    async def resolve(self, ref: str) -> CommitSha:
        self._check("resolve")
        return await self.inner.resolve(ref)

    async def create_branch(self, name: str, at: CommitSha, *, force: bool = False) -> None:
        self._check("create_branch")
        await self.inner.create_branch(name, at, force=force)

    async def delete_branch(self, name: str) -> None:
        self._check("delete_branch")
        await self.inner.delete_branch(name)

    async def head(self, worktree: Worktree) -> CommitSha:
        self._check("head")
        return await self.inner.head(worktree)

    async def add_worktree(self, branch: str, base: CommitSha, at: Path) -> Worktree:
        self._check("add_worktree")
        return await self.inner.add_worktree(branch, base, at)

    async def attach_worktree(self, branch: str, at: Path) -> Worktree:
        self._check("attach_worktree")
        return await self.inner.attach_worktree(branch, at)

    async def remove_worktree(self, worktree: Worktree, *, force: bool = False) -> None:
        self._check("remove_worktree")
        await self.inner.remove_worktree(worktree, force=force)

    async def list_worktrees(self) -> tuple[Worktree, ...]:
        self._check("list_worktrees")
        return await self.inner.list_worktrees()

    async def status(self, worktree: Worktree) -> WorkingStatus:
        self._check("status")
        return await self.inner.status(worktree)

    async def snapshot(self, worktree: Worktree, *, message: str = "lj-snapshot") -> Snapshot:
        self._check("snapshot")
        return await self.inner.snapshot(worktree, message=message)

    async def merge_base(self, left: CommitSha, right: CommitSha) -> CommitSha | None:
        self._check("merge_base")
        return await self.inner.merge_base(left, right)

    async def merge_tree(
        self, ours: CommitSha, theirs: CommitSha, *, base: CommitSha | None = None
    ) -> MergeTreeResult:
        self._check("merge_tree")
        return await self.inner.merge_tree(ours, theirs, base=base)

    async def merge(self, branch: str, into: str, *, message: str) -> MergeOutcome:
        self._check("merge")
        return await self.inner.merge(branch, into, message=message)

    async def rebase(self, worktree: Worktree, onto: CommitSha) -> RebaseOutcome:
        self._check("rebase")
        return await self.inner.rebase(worktree, onto)

    async def changes(self, base: CommitSha, tip: CommitSha) -> tuple[FileChange, ...]:
        self._check("changes")
        return await self.inner.changes(base, tip)

    async def commits_between(self, base: CommitSha, tip: CommitSha) -> int:
        self._check("commits_between")
        return await self.inner.commits_between(base, tip)

    async def read_blob(self, commit: CommitSha, path: RepoPath) -> bytes | None:
        self._check("read_blob")
        return await self.inner.read_blob(commit, path)

    async def list_files(self, commit: CommitSha) -> tuple[RepoPath, ...]:
        self._check("list_files")
        return await self.inner.list_files(commit)

    async def churn(self, since: str = "200") -> dict[RepoPath, int]:
        self._check("churn")
        return await self.inner.churn(since)

    async def commit_all(self, worktree: Worktree, message: str) -> CommitSha | None:
        self._check("commit_all")
        return await self.inner.commit_all(worktree, message)


@dataclass
class FaultyGate:
    """A :class:`Gate` that can raise, hang or report on demand."""

    inner: Gate
    faults: list[BaseException | None] = field(default_factory=list)
    reports: list[GateReport] = field(default_factory=list)
    runs: int = 0

    async def run(self, worktree: Worktree) -> GateReport:
        self.runs += 1
        if self.faults:
            error = self.faults.pop(0)
            if error is not None:
                raise error
        if self.reports:
            return self.reports.pop(0)
        return await self.inner.run(worktree)


@dataclass
class FaultyRunner:
    """A :class:`WorkerRunner` that blows up for named tasks and delegates the rest.

    This is how "one agent crashing must not stop the stand" gets tested: the peers run
    against the real runner in the same stand while one task raises.
    """

    inner: WorkerRunner
    explode_on: dict[str, BaseException] = field(default_factory=dict)
    name: str = "faulty"

    async def preflight(self, services: Services) -> None:
        await self.inner.preflight(services)

    async def run(self, workstream: Workstream, spec: TaskSpec, services: Services) -> WorkerOutput:
        error = self.explode_on.get(spec.task_id)
        if error is not None:
            raise error
        return await self.inner.run(workstream, spec, services)


@dataclass(frozen=True)
class StubUsageTotals:
    """The shape of 0002's ``UsageTotals``, as far as ``Budget`` cares about it."""

    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0
    steps: int = 0
    wall_clock: timedelta = timedelta(0)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class StubUsage:
    """A ``UsageLedger`` whose totals climb by ``per_call`` every time they are read.

    Spend that only grows when someone looks at it is exactly what a budget check has
    to cope with, and it makes "the stand launched work, then went over" reproducible.
    """

    per_call: int = 0
    fixed: int | None = None
    reads: int = 0

    def totals(self) -> StubUsageTotals:
        self.reads += 1
        spent = self.fixed if self.fixed is not None else self.per_call * self.reads
        return StubUsageTotals(output_tokens=spent, requests=self.reads, steps=self.reads)

    def for_workstream(self, workstream: WorkstreamId) -> StubUsageTotals:
        _ = workstream
        return StubUsageTotals()
