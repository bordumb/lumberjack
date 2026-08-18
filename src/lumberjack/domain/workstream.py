"""Worktrees, workstreams and stand-level configuration."""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lumberjack.domain.accord import NegotiationLimits
from lumberjack.ids import AgentId, CommitSha, RepoPath, StandId, TaskId, WorkstreamId

__all__ = [
    "ArbitrationMode",
    "Budget",
    "DriftStatus",
    "Snapshot",
    "StandConfig",
    "Workstream",
    "Worktree",
]


class Worktree(BaseModel):
    """A git worktree on disk, checked out to a branch of the stand's repo."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: Path
    branch: str = Field(min_length=1)
    base: CommitSha

    def __str__(self) -> str:
        return f"{self.branch} @ {self.path}"


class Snapshot(BaseModel):
    """An ephemeral commit capturing a worktree's state, including uncommitted work.

    Written with a throwaway index so nothing in the working directory is disturbed,
    and never referenced by a branch, so git will collect it in due course.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    commit: CommitSha
    tree: str
    dirty: bool = Field(description="Whether the worktree had uncommitted changes.")
    paths: tuple[RepoPath, ...] = ()


class DriftStatus(BaseModel):
    """How far a workstream has fallen behind the integration branch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    behind: int = Field(default=0, ge=0)
    integration_head: CommitSha | None = None
    rebase_clean: bool | None = Field(
        default=None,
        description="Oracle verdict on rebasing onto integration; None when unknown.",
    )

    def advice(self, threshold: int) -> str | None:
        if self.behind == 0:
            return None
        if self.rebase_clean is False:
            return (
                f"{self.behind} commit(s) behind integration and the rebase will conflict; "
                "resolve before requesting a land"
            )
        if self.behind >= threshold:
            return f"{self.behind} commit(s) behind integration; rebase soon"
        return None


class Workstream(BaseModel):
    """One agent + one worktree + one branch + one task.  The unit of parallelism."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workstream_id: WorkstreamId
    stand: StandId
    agent: AgentId
    task: TaskId
    worktree: Worktree
    tip: CommitSha | None = None
    drift: DriftStatus = DriftStatus()
    active: bool = True


class ArbitrationMode(StrEnum):
    """Which :class:`ArbitrationPolicy` a stand runs."""

    PARTITION = "partition"
    FIRST_WRITER_WINS = "first_writer_wins"
    PEER_NEGOTIATION = "peer_negotiation"
    FOREMAN_RULES = "foreman_rules"
    HYBRID = "hybrid"


class Budget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_steps_per_task: int = Field(default=40, ge=1)
    max_wall_clock: timedelta = timedelta(hours=2)
    max_total_tokens: int | None = Field(default=None, ge=1)


class StandConfig(BaseModel):
    """Everything a stand needs to run.  Serializable, so ``lj`` can round-trip it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: Path = Path()
    base_ref: str = "main"
    max_parallel: int = Field(default=6, ge=1, le=16)
    arbitration: ArbitrationMode = ArbitrationMode.HYBRID
    model: str = "anthropic:claude-opus-5"
    foreman_model: str | None = None
    worktree_root: Path = Path(".lumberjack/worktrees")
    state_root: Path = Path(".lumberjack")
    integration_prefix: str = "integration"
    branch_prefix: str = "lj"
    lease_ttl: timedelta = timedelta(minutes=30)
    auto_rebase_after: int = Field(default=3, ge=1)
    bounce_limit: int = Field(default=3, ge=1)
    oracle_debounce: timedelta = timedelta(seconds=3)
    sensor_debounce: timedelta = timedelta(milliseconds=750)
    digest_token_cap: int = Field(default=1200, ge=200)
    digest_note_cap: int = Field(default=8, ge=0)
    negotiation: NegotiationLimits = NegotiationLimits()
    budget: Budget = Budget()
    protected_paths: tuple[str, ...] = (".lumberjack/**", ".git/**")
    gate_commands: tuple[tuple[str, ...], ...] = (
        ("uv", "run", "ruff", "check", "."),
        ("uv", "run", "ty", "check"),
        ("uv", "run", "pytest", "-q"),
    )

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.worktree_root.is_absolute() != self.state_root.is_absolute():
            msg = "worktree_root and state_root must both be relative or both absolute"
            raise ValueError(msg)
        return self

    def integration_branch(self, stand: StandId) -> str:
        return f"{self.integration_prefix}/{stand}"

    def workstream_branch(self, stand: StandId, task: TaskId) -> str:
        return f"{self.branch_prefix}/{stand}/{task}"

    def resolved_worktree_root(self) -> Path:
        root = self.worktree_root
        return root if root.is_absolute() else self.repo / root

    def resolved_state_root(self) -> Path:
        root = self.state_root
        return root if root.is_absolute() else self.repo / root
