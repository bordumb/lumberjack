"""Retrying git, by wrapping the port rather than editing every call site.

Every worktree in a stand shares one object store, so ``index.lock`` and ref-lock
contention between siblings is not hypothetical -- it is the normal cost of running six
agents against one repository.  Those failures are worth one more try after a short,
jittered wait.  Nothing else is.

Two rules make this safe:

* the allowlist is :data:`~lumberjack.domain.errors.GIT_TRANSIENT_MARKERS`, and every
  entry in it names a lock;
* **a conflict is never retried.**  ``merge_tree``, ``merge`` and ``rebase`` report
  conflicts as an *outcome*, not an exception, so a conflicted merge cannot reach the
  retry path at all -- and this wrapper passes those results straight through.

Shaped after ``adapters/projecting.py``: a dataclass holding ``inner`` that delegates
every method, so the core is untouched and the behaviour is one reviewable file.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from lumberjack.domain.errors import Backoff
from lumberjack.ports.clock import Clock
from lumberjack.ports.git import GitBackend, GitError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from lumberjack.domain.vcs import (
        FileChange,
        MergeOutcome,
        MergeTreeResult,
        RebaseOutcome,
        WorkingStatus,
    )
    from lumberjack.domain.workstream import Snapshot, Worktree
    from lumberjack.ids import CommitSha, RepoPath

__all__ = ["RetryingGit"]

log = logging.getLogger(__name__)


@dataclass(slots=True)
class RetryingGit:
    """A :class:`GitBackend` that retries lock contention and nothing else."""

    inner: GitBackend
    clock: Clock | None = None
    """Used only to sleep between attempts; ``None`` sleeps on the event loop."""
    backoff: Backoff = field(default_factory=Backoff)
    attempts: int = 0
    """Total retried attempts across this backend's life, for tests and for the log."""

    async def _retry[T](self, what: str, call: Callable[[], Awaitable[T]]) -> T:
        attempt = 1
        while True:
            try:
                return await call()
            except GitError as error:
                if not error.transient or attempt >= self.backoff.attempts:
                    raise
                delay = self.backoff.delay(attempt)
                self.attempts += 1
                log.warning(
                    "git %s hit lock contention (attempt %d/%d), retrying in %.0fms: %s",
                    what,
                    attempt,
                    self.backoff.attempts,
                    delay.total_seconds() * 1000,
                    error.stderr.strip()[:200],
                )
                await self._sleep(delay)
                attempt += 1

    async def _sleep(self, delay: timedelta) -> None:
        if self.clock is not None:
            await self.clock.sleep(delay)
            return
        await asyncio.sleep(delay.total_seconds())

    # -- refs --------------------------------------------------------------------------

    async def resolve(self, ref: str) -> CommitSha:
        return await self._retry("rev-parse", lambda: self.inner.resolve(ref))

    async def create_branch(self, name: str, at: CommitSha, *, force: bool = False) -> None:
        await self._retry("branch", lambda: self.inner.create_branch(name, at, force=force))

    async def delete_branch(self, name: str) -> None:
        await self._retry("branch -D", lambda: self.inner.delete_branch(name))

    async def head(self, worktree: Worktree) -> CommitSha:
        return await self._retry("rev-parse HEAD", lambda: self.inner.head(worktree))

    # -- worktrees ---------------------------------------------------------------------

    async def add_worktree(self, branch: str, base: CommitSha, at: Path) -> Worktree:
        return await self._retry(
            "worktree add", lambda: self.inner.add_worktree(branch, base, at)
        )

    async def attach_worktree(self, branch: str, at: Path) -> Worktree:
        return await self._retry("worktree add", lambda: self.inner.attach_worktree(branch, at))

    async def remove_worktree(self, worktree: Worktree, *, force: bool = False) -> None:
        await self._retry(
            "worktree remove", lambda: self.inner.remove_worktree(worktree, force=force)
        )

    async def list_worktrees(self) -> tuple[Worktree, ...]:
        return await self._retry("worktree list", self.inner.list_worktrees)

    # -- working state -----------------------------------------------------------------

    async def status(self, worktree: Worktree) -> WorkingStatus:
        return await self._retry("status", lambda: self.inner.status(worktree))

    async def snapshot(self, worktree: Worktree, *, message: str = "lj-snapshot") -> Snapshot:
        return await self._retry(
            "snapshot", lambda: self.inner.snapshot(worktree, message=message)
        )

    # -- merging -----------------------------------------------------------------------

    async def merge_base(self, left: CommitSha, right: CommitSha) -> CommitSha | None:
        return await self._retry("merge-base", lambda: self.inner.merge_base(left, right))

    async def merge_tree(
        self, ours: CommitSha, theirs: CommitSha, *, base: CommitSha | None = None
    ) -> MergeTreeResult:
        """Never retried on a conflict: a conflict is the answer, not a failure.

        ``merge-tree`` reports one by returning a :class:`MergeTreeResult` with
        ``clean=False``, which is a value and cannot reach the retry path.
        """
        return await self._retry(
            "merge-tree", lambda: self.inner.merge_tree(ours, theirs, base=base)
        )

    async def merge(self, branch: str, into: str, *, message: str) -> MergeOutcome:
        return await self._retry(
            "merge", lambda: self.inner.merge(branch, into, message=message)
        )

    async def rebase(self, worktree: Worktree, onto: CommitSha) -> RebaseOutcome:
        return await self._retry("rebase", lambda: self.inner.rebase(worktree, onto))

    # -- history -----------------------------------------------------------------------

    async def changes(self, base: CommitSha, tip: CommitSha) -> tuple[FileChange, ...]:
        return await self._retry("diff", lambda: self.inner.changes(base, tip))

    async def commits_between(self, base: CommitSha, tip: CommitSha) -> int:
        return await self._retry("rev-list", lambda: self.inner.commits_between(base, tip))

    async def read_blob(self, commit: CommitSha, path: RepoPath) -> bytes | None:
        return await self._retry("cat-file", lambda: self.inner.read_blob(commit, path))

    async def list_files(self, commit: CommitSha) -> tuple[RepoPath, ...]:
        return await self._retry("ls-tree", lambda: self.inner.list_files(commit))

    async def churn(self, since: str = "200") -> dict[RepoPath, int]:
        return await self._retry("log", lambda: self.inner.churn(since))

    async def commit_all(self, worktree: Worktree, message: str) -> CommitSha | None:
        return await self._retry("commit", lambda: self.inner.commit_all(worktree, message))
