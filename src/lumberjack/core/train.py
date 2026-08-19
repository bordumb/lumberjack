"""The merge train.

Nothing lands directly on ``main``.  Entries are integrated one at a time onto the
stand's integration branch, and the oracle pre-check comes *before* the gate so a
doomed merge never burns a test run.  A bounce returns the task to the same worker
with the failure attached as new context, up to the configured limit.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from lumberjack.core.oracle import ConflictOracle
from lumberjack.core.projections import Projections
from lumberjack.core.tasks import record_transition
from lumberjack.domain.events import (
    Bounced,
    GateRun,
    LandRequested,
    WorkstreamLanded,
)
from lumberjack.domain.gate import GateReport
from lumberjack.domain.task import AwaitingIntegration, BlockReason, Running
from lumberjack.domain.workstream import StandConfig
from lumberjack.ids import CommitSha, RepoPath, WorkstreamId
from lumberjack.ports.clock import Clock
from lumberjack.ports.gate import Gate
from lumberjack.ports.git import GitBackend
from lumberjack.ports.ledger import Ledger

__all__ = ["LandOutcome", "MergeTrain", "TrainPosition"]


class TrainPosition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    workstream: WorkstreamId
    position: int
    ahead: tuple[WorkstreamId, ...] = ()


class LandOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    workstream: WorkstreamId
    status: Literal["landed", "bounced", "blocked", "skipped"]
    merge: CommitSha | None = None
    why: str = ""
    conflicted: tuple[RepoPath, ...] = ()
    report: GateReport | None = None


@dataclass(slots=True)
class MergeTrain:
    git: GitBackend
    ledger: Ledger
    projections: Projections
    oracle: ConflictOracle
    gate: Gate
    clock: Clock
    config: StandConfig
    queue: list[WorkstreamId] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    """The train is a train: exactly one integration at a time, from any caller."""

    async def request(self, workstream: WorkstreamId, tip: CommitSha) -> TrainPosition:
        if workstream not in self.queue:
            self.queue.append(workstream)
        position = self.queue.index(workstream) + 1
        await self.ledger.append(
            LandRequested(workstream=workstream, tip=tip, position=position),
            actor=self.projections.agent_of(workstream) or "system",
        )
        found = self.projections.workstreams.get(workstream)
        if found is not None:
            self.projections.workstreams[workstream] = found.model_copy(update={"tip": tip})
        return TrainPosition(
            workstream=workstream,
            position=position,
            ahead=tuple(self.queue[: position - 1]),
        )

    async def run_once(self) -> LandOutcome | None:
        """Integrate the head of the queue, if there is one."""
        async with self._lock:
            if not self.queue:
                return None
            workstream = self.queue[0]
            outcome = await self._land(workstream)
            if self.queue and self.queue[0] == workstream:
                self.queue.pop(0)
            return outcome

    async def drain(self) -> tuple[LandOutcome, ...]:
        outcomes: list[LandOutcome] = []
        while self.queue:
            outcome = await self.run_once()
            if outcome is None:
                break
            outcomes.append(outcome)
        return tuple(outcomes)

    async def _land(self, workstream: WorkstreamId) -> LandOutcome:
        found = self.projections.workstreams.get(workstream)
        task = self.projections.tasks.get(found.task) if found is not None else None
        if found is None or not isinstance(task, AwaitingIntegration):
            return LandOutcome(
                workstream=workstream,
                status="skipped",
                why="workstream is not awaiting integration",
            )

        # 1. A person is still waiting on this. Nothing else in the queue outranks that.
        unresolved = self.projections.comments_for(workstream, unresolved_only=True)
        if unresolved:
            listed = "; ".join(item.render() for item in unresolved[:3])
            return await self._bounce(
                task,
                why=f"{len(unresolved)} unresolved review comment(s): {listed}",
            )

        # 2. Oracle pre-check.  Never burn a gate run on a merge git has already refused.
        clean, conflicted = await self.oracle.would_land_cleanly(workstream)
        if not clean:
            return await self._bounce(
                task,
                why="conflicts with the integration branch; rebase and resolve",
                conflicted=conflicted,
            )

        # 3. Rebase onto the current integration head.
        head = self.projections.integration_head
        if head is not None:
            rebase = await self.git.rebase(found.worktree, head)
            if not rebase.ok:
                return await self._bounce(
                    task,
                    why=f"rebase onto integration failed: {rebase.status} {rebase.detail}"[:400],
                    conflicted=rebase.conflicted,
                )

        # 4. Gate.
        report = await self.gate.run(found.worktree)
        await self.ledger.append(GateRun(workstream=workstream, report=report))
        if not report.passed:
            return await self._bounce(task, why=report.render(600), report=report)

        # 5. Merge into integration.
        merged = await self.git.merge(
            found.worktree.branch,
            self.projections.integration_branch,
            message=f"land {task.spec.title} ({task.spec.task_id})",
        )
        if not merged.ok or merged.head is None:
            return await self._bounce(
                task,
                why=f"merge into integration failed: {merged.status} {merged.detail}"[:400],
                conflicted=merged.conflicted,
            )

        now: datetime = self.clock.now()
        landed = task.land(merged.head, now)
        await record_transition(self.ledger, self.projections, landed, actor=task.agent)
        await self.ledger.append(
            WorkstreamLanded(
                workstream=workstream,
                task=task.spec.task_id,
                merge=merged.head,
                integration_head=merged.head,
            )
        )
        self.projections.integration_head = merged.head
        return LandOutcome(workstream=workstream, status="landed", merge=merged.head, report=report)

    async def _bounce(
        self,
        task: AwaitingIntegration,
        *,
        why: str,
        report: GateReport | None = None,
        conflicted: tuple[RepoPath, ...] = (),
    ) -> LandOutcome:
        attempt = task.attempts + 1
        await self.ledger.append(
            Bounced(
                workstream=task.workstream,
                task=task.spec.task_id,
                why=why,
                report=report,
                attempt=attempt,
            )
        )
        if attempt > self.config.bounce_limit:
            blocked = task.block(BlockReason.GATE_EXHAUSTED, why)
            await record_transition(
                self.ledger, self.projections, blocked, actor=task.agent, detail=why
            )
            return LandOutcome(
                workstream=task.workstream,
                status="blocked",
                why=f"bounce limit reached ({self.config.bounce_limit}): {why}",
                conflicted=conflicted,
                report=report,
            )
        running: Running = task.bounce(report or GateReport())
        await record_transition(
            self.ledger, self.projections, running, actor=task.agent, detail=why
        )
        return LandOutcome(
            workstream=task.workstream,
            status="bounced",
            why=why,
            conflicted=conflicted,
            report=report,
        )

    async def rebase_drifted(self) -> tuple[WorkstreamId, ...]:
        """Silently rebase clean, drifted workstreams; leave dirty ones to their agent.

        Takes the train's lock: this rewrites worktrees, and doing so while an
        integration is mid-rebase moves the ground under it, which surfaces as a
        spurious bounce on work that would have landed.
        """
        async with self._lock:
            head = self.projections.integration_head
            if head is None:
                return ()
            rebased: list[WorkstreamId] = []
            for found in self.projections.active_workstreams():
                drift = found.drift
                if drift.behind < self.config.auto_rebase_after or drift.rebase_clean is not True:
                    continue
                outcome = await self.git.rebase(found.worktree, head)
                if outcome.ok:
                    rebased.append(found.workstream_id)
            return tuple(rebased)
