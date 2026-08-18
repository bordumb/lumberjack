"""Executing rulings.

Accords are *applied by the core*, not honoured on trust.  A ``Split`` rewrites both
sides' leases, a ``Defer`` parks a workstream and schedules its rebase, an ``Extract``
files the follow-up work.  An agent that signs an accord and then ignores it will find
the lease it needs is no longer held.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from lumberjack.core.board import LedgerBlackboard, LedgerMessageBus
from lumberjack.core.broker import LeaseBroker
from lumberjack.core.projections import Projections
from lumberjack.domain.accord import (
    Accord,
    Adopt,
    Defer,
    EscalateToForeman,
    Extract,
    Resolution,
    Ruling,
    Split,
)
from lumberjack.domain.claim import AccessMode, Claim, LeaseGranted, PathScope, scopes_overlap
from lumberjack.domain.events import (
    AccordSigned,
    ConflictCleared,
    DirectiveIssued,
    ResolutionApplied,
)
from lumberjack.domain.note import Topic
from lumberjack.ids import AgentId, WorkstreamId, glob_pattern
from lumberjack.ports.clock import Clock
from lumberjack.ports.ledger import Ledger

__all__ = ["RulingExecutor"]


@dataclass(slots=True)
class RulingExecutor:
    ledger: Ledger
    projections: Projections
    broker: LeaseBroker
    board: LedgerBlackboard
    bus: LedgerMessageBus
    clock: Clock
    parked: dict[WorkstreamId, WorkstreamId] = field(default_factory=dict)
    """workstream -> the workstream it is waiting on."""

    async def apply(self, ruling: Ruling) -> str:
        conflict_id = ruling.conflict_id
        report = self.projections.conflicts.get(conflict_id)
        if isinstance(ruling, Accord):
            await self.ledger.append(AccordSigned(accord=ruling))
            source = "accord"
        else:
            await self.ledger.append(DirectiveIssued(directive=ruling))
            source = "directive"

        detail = await self._execute(ruling.resolution, ruling)
        await self.ledger.append(
            ResolutionApplied(
                conflict_id=conflict_id,
                resolution=ruling.resolution,
                source=source,  # type: ignore[arg-type]
                accord_id=ruling.accord_id if isinstance(ruling, Accord) else None,
                detail=detail,
            )
        )
        if report is not None and not isinstance(ruling.resolution, EscalateToForeman):
            await self.ledger.append(
                ConflictCleared(
                    conflict_id=conflict_id,
                    between=report.between,
                    why=f"{source}: {ruling.resolution.kind}",
                )
            )
        return detail

    async def _execute(self, resolution: Resolution, ruling: Ruling) -> str:
        match resolution:
            case Defer():
                return await self._defer(resolution, ruling)
            case Split():
                return await self._split(resolution, ruling)
            case Extract():
                return await self._extract(resolution, ruling)
            case Adopt():
                return await self._adopt(resolution, ruling)
            case EscalateToForeman():
                return f"escalated: {resolution.reason}"

    async def _defer(self, resolution: Defer, ruling: Ruling) -> str:
        workstream = self.projections.workstream_of(resolution.yielding)
        if workstream is not None:
            self.parked[workstream] = resolution.until
            await self._release_overlapping(workstream, ruling)
        await self.bus.send(
            frm=AgentId("system"),
            to=resolution.yielding,
            subject="you are deferring on a contested scope",
            body=(
                f"Wait for {resolution.until} to land, then rebase and re-verify. {resolution.note}"
            ),
        )
        return f"{resolution.yielding} defers to {resolution.until}"

    async def _split(self, resolution: Split, ruling: Ruling) -> str:
        granted: list[str] = []
        for agent, scope in resolution.assignments:
            workstream = self.projections.workstream_of(agent)
            if workstream is None:
                continue
            await self._release_overlapping(workstream, ruling)
            found = self.projections.workstreams[workstream]
            decision = await self.broker.request(
                Claim(
                    claimant=agent,
                    workstream=workstream,
                    task=found.task,
                    scope=scope,
                    mode=AccessMode.EDIT,
                    rationale=f"split assigned by {ruling.kind} on {ruling.conflict_id}",
                )
            )
            if isinstance(decision, LeaseGranted):
                granted.append(f"{agent} -> {scope.describe()}")
        return "split: " + "; ".join(granted)

    async def _extract(self, resolution: Extract, ruling: Ruling) -> str:
        moved = ", ".join(str(symbol) for symbol in resolution.moved)
        await self.board.post(
            author=AgentId("system"),
            topic=Topic.DECISIONS,
            body=(
                f"{moved} move to {resolution.new_module}, owned by {resolution.owner} "
                f"(resolution of {ruling.conflict_id}). Import from there rather than "
                "redefining."
            ),
            scope=PathScope(patterns=(glob_pattern(resolution.new_module),)),
            pins=resolution.moved,
        )
        workstream = self.projections.workstream_of(resolution.owner)
        if workstream is not None:
            found = self.projections.workstreams[workstream]
            await self.broker.request(
                Claim(
                    claimant=resolution.owner,
                    workstream=workstream,
                    task=found.task,
                    scope=PathScope(patterns=(glob_pattern(resolution.new_module),)),
                    mode=AccessMode.EXCLUSIVE,
                    rationale=f"owns extracted module per {ruling.conflict_id}",
                )
            )
        return f"extract {moved} into {resolution.new_module} owned by {resolution.owner}"

    async def _adopt(self, resolution: Adopt, ruling: Ruling) -> str:
        await self.bus.send(
            frm=AgentId("system"),
            to=resolution.superseded,
            subject="adopt the canonical implementation",
            body=(
                f"{resolution.canonical}'s implementation is canonical for "
                f"{ruling.conflict_id}. Drop yours and import theirs. {resolution.note}"
            ),
        )
        return f"{resolution.superseded} adopts {resolution.canonical}"

    async def _release_overlapping(self, workstream: WorkstreamId, ruling: Ruling) -> None:
        report = self.projections.conflicts.get(ruling.conflict_id)
        now: datetime = self.clock.now()
        for lease in self.projections.leases_of(workstream, now):
            touches = report is None or any(lease.scope.matches_path(path) for path in report.paths)
            overlaps_other = report is not None and any(
                scopes_overlap(lease.scope, other.scope)
                for other in self.projections.leases_of(report.other(workstream), now)
            )
            if touches or overlaps_other:
                await self.broker.release(lease.lease_id)

    def is_parked(self, workstream: WorkstreamId) -> bool:
        waiting_on = self.parked.get(workstream)
        if waiting_on is None:
            return False
        found = self.projections.workstreams.get(waiting_on)
        if found is None or found.task in self.projections.landed:
            self.parked.pop(workstream, None)
            return False
        return True
