"""Operating a stand after it has started: pause, continue, rename, delete.

Two of these are not what their names suggest, and the difference matters.

*Continue* cannot resume a halted stand. Halting kills the sessions and the supervisor
exits, so there is no process to un-pause. What it honestly means is: start a new stand
from this one's branches, carrying its tasks forward. The link between the two is
recorded so the lineage stays visible.

*Delete* destroys three separate things -- the ledger, the worktrees, and the branches
-- and only the first is certainly worthless. A branch can hold work that never landed,
so this reports what it is about to lose and refuses to take unlanded work without
being told to.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from lumberjack.core.projections import Projections
from lumberjack.domain.task import TaskSpec
from lumberjack.domain.workstream import StandConfig
from lumberjack.ids import StandId
from lumberjack.ports.git import GitBackend, GitError

__all__ = ["DeletionPlan", "StandControl", "stand_alive", "supervisor_alive"]


def supervisor_alive(pid: int | None) -> bool | None:
    """Whether the process that started a stand still exists.

    ``None`` when there is nothing to check -- a stand recorded before pids were
    written keeps the older, more forgiving reading rather than being called dead.
    """
    if pid is None:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class DeletionPlan(BaseModel):
    """What deleting a stand would destroy, counted before anything is destroyed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stand: StandId
    ledger_bytes: int = 0
    worktrees: tuple[str, ...] = ()
    branches: tuple[str, ...] = ()
    unlanded: tuple[str, ...] = ()
    """Branches holding commits that never reached the base branch."""
    running: bool = False

    @property
    def safe(self) -> bool:
        return not self.running and not self.unlanded

    def describe(self) -> str:
        parts = [f"ledger {self.ledger_bytes // 1024}KB"]
        if self.worktrees:
            parts.append(f"{len(self.worktrees)} worktree(s)")
        if self.branches:
            parts.append(f"{len(self.branches)} branch(es)")
        if self.unlanded:
            parts.append(f"{len(self.unlanded)} holding unlanded work")
        return ", ".join(parts)


@dataclass(slots=True)
class StandControl:
    repo: Path
    config: StandConfig

    def _state(self, stand: StandId) -> Path:
        return self.config.resolved_state_root() / stand

    async def plan_deletion(
        self, stand: StandId, projections: Projections, git: GitBackend
    ) -> DeletionPlan:
        state = self._state(stand)
        ledger = state / "ledger.db"

        worktrees = tuple(
            str(item.worktree.path)
            for item in projections.workstreams.values()
            if item.worktree.path.exists()
        )
        branches = tuple(
            [item.worktree.branch for item in projections.workstreams.values()]
            + ([projections.integration_branch] if projections.integration_branch else [])
        )

        unlanded: list[str] = []
        base = await git.resolve(self.config.base_ref)
        for branch in branches:
            try:
                tip = await git.resolve(branch)
            except GitError:
                continue  # a branch that is already gone is nothing to lose
            ahead = await git.commits_between(base, tip)
            if ahead:
                unlanded.append(f"{branch} (+{ahead})")

        return DeletionPlan(
            stand=stand,
            ledger_bytes=ledger.stat().st_size if ledger.exists() else 0,
            worktrees=worktrees,
            branches=branches,
            unlanded=tuple(unlanded),
            running=supervisor_alive(projections.pid) is True,
        )

    async def delete(
        self,
        stand: StandId,
        projections: Projections,
        git: GitBackend,
        *,
        drop_branches: bool = False,
    ) -> DeletionPlan:
        """Remove the stand. Branches are kept unless explicitly dropped."""
        plan = await self.plan_deletion(stand, projections, git)
        for item in projections.workstreams.values():
            if item.worktree.path.exists():
                try:
                    await git.remove_worktree(item.worktree, force=True)  # type: ignore[attr-defined]
                except Exception:
                    shutil.rmtree(item.worktree.path, ignore_errors=True)
        if drop_branches:
            for branch in plan.branches:
                await git.delete_branch(branch)
        shutil.rmtree(self._state(stand), ignore_errors=True)
        return plan

    @staticmethod
    def tasks_of(projections: Projections) -> tuple[TaskSpec, ...]:
        """The tasks a stand was given, so a continuation can carry them forward."""
        return tuple(projections.specs.values())


def stand_alive(
    pid: int | None,
    ledger: Path,
    *,
    idle_after: timedelta = timedelta(minutes=3),
) -> bool | None:
    """Whether a stand is still being worked on.

    The process check is the reliable signal, but runs recorded before pids were
    written have none. For those, fall back to evidence rather than optimism: an
    append-only log that has not been appended to in minutes is not a live run.
    """
    by_process = supervisor_alive(pid)
    if by_process is not None:
        return by_process
    try:
        idle = datetime.now(UTC) - datetime.fromtimestamp(ledger.stat().st_mtime, UTC)
    except OSError:
        return None
    return idle < idle_after
