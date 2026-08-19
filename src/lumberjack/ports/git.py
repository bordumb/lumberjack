"""The git boundary.

Only two operations here are unusual, and both matter a great deal:

``snapshot``
    Captures a worktree including *uncommitted* work, by writing a throwaway index
    and an unreferenced commit.  Agents therefore do not have to commit to become
    visible to the oracle.

``merge_tree``
    Performs a real merge between two commits with no worktree and no checkout
    (``git merge-tree --write-tree``).  This is the ground truth for conflict.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pathlib import Path

    from lumberjack.domain.vcs import (
        FileChange,
        MergeOutcome,
        MergeTreeResult,
        RebaseOutcome,
        WorkingStatus,
    )
    from lumberjack.domain.workstream import Snapshot, Worktree
    from lumberjack.ids import CommitSha, RepoPath

__all__ = ["GitBackend", "GitError"]


class GitError(RuntimeError):
    """A git invocation failed in a way the caller cannot model."""

    def __init__(self, command: tuple[str, ...], code: int, stderr: str) -> None:
        super().__init__(f"git {' '.join(command)} failed ({code}): {stderr.strip()[:400]}")
        self.command = command
        self.code = code
        self.stderr = stderr


class GitBackend(Protocol):
    async def resolve(self, ref: str) -> CommitSha: ...

    async def create_branch(self, name: str, at: CommitSha, *, force: bool = False) -> None: ...

    async def delete_branch(self, name: str) -> None: ...

    async def add_worktree(self, branch: str, base: CommitSha, at: Path) -> Worktree: ...

    async def attach_worktree(self, branch: str, at: Path) -> Worktree: ...

    """Check out an *existing* branch into a worktree, continuing its history."""

    async def remove_worktree(self, worktree: Worktree, *, force: bool = False) -> None: ...

    async def list_worktrees(self) -> tuple[Worktree, ...]: ...

    async def status(self, worktree: Worktree) -> WorkingStatus: ...

    async def snapshot(self, worktree: Worktree, *, message: str = "lj-snapshot") -> Snapshot: ...

    """Commit-tree the worktree's full state without disturbing the working directory."""

    async def head(self, worktree: Worktree) -> CommitSha: ...

    async def merge_base(self, left: CommitSha, right: CommitSha) -> CommitSha | None: ...

    async def merge_tree(
        self, ours: CommitSha, theirs: CommitSha, *, base: CommitSha | None = None
    ) -> MergeTreeResult: ...

    async def changes(self, base: CommitSha, tip: CommitSha) -> tuple[FileChange, ...]: ...

    async def commits_between(self, base: CommitSha, tip: CommitSha) -> int: ...

    async def read_blob(self, commit: CommitSha, path: RepoPath) -> bytes | None: ...

    async def list_files(self, commit: CommitSha) -> tuple[RepoPath, ...]: ...

    async def churn(self, since: str = "200") -> dict[RepoPath, int]: ...

    async def rebase(self, worktree: Worktree, onto: CommitSha) -> RebaseOutcome: ...

    async def merge(self, branch: str, into: str, *, message: str) -> MergeOutcome: ...

    async def commit_all(self, worktree: Worktree, message: str) -> CommitSha | None: ...
