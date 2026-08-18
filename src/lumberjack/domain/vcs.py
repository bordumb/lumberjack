"""Typed results of the git operations the harness depends on."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from lumberjack.ids import CommitSha, RepoPath, TreeSha

__all__ = [
    "ChangeKind",
    "FileChange",
    "MergeOutcome",
    "MergeTreeResult",
    "RebaseOutcome",
    "WorkingStatus",
]


class ChangeKind(StrEnum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"
    TYPE_CHANGED = "type_changed"

    @property
    def is_structural(self) -> bool:
        """Structural change defeats line-based merging and must be EXCLUSIVE."""
        return self in (ChangeKind.RENAMED, ChangeKind.DELETED, ChangeKind.TYPE_CHANGED)


class FileChange(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: RepoPath
    kind: ChangeKind
    old_path: RepoPath | None = None
    insertions: int = Field(default=0, ge=0)
    deletions: int = Field(default=0, ge=0)

    @property
    def lines_changed(self) -> int:
        return self.insertions + self.deletions


class MergeTreeResult(BaseModel):
    """The result of ``git merge-tree --write-tree``: a real merge, no worktree touched."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    clean: bool
    tree: TreeSha | None = None
    conflicted: tuple[RepoPath, ...] = ()
    messages: str = ""
    merge_base: CommitSha | None = None

    @property
    def conflict_count(self) -> int:
        return len(self.conflicted)


class RebaseOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["rebased", "already_current", "conflicted", "failed"]
    head: CommitSha | None = None
    conflicted: tuple[RepoPath, ...] = ()
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("rebased", "already_current")


class MergeOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["merged", "fast_forwarded", "up_to_date", "conflicted", "failed"]
    head: CommitSha | None = None
    conflicted: tuple[RepoPath, ...] = ()
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("merged", "fast_forwarded", "up_to_date")


class WorkingStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    changes: tuple[FileChange, ...] = ()
    untracked: tuple[RepoPath, ...] = ()

    @property
    def dirty(self) -> bool:
        return bool(self.changes or self.untracked)

    @property
    def paths(self) -> tuple[RepoPath, ...]:
        return (*(change.path for change in self.changes), *self.untracked)
