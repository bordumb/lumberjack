"""The lease broker -- the intent plane.

The grant matrix is the whole policy:

==============  ====  ====  =========
held \\ wanted   read  edit  exclusive
==============  ====  ====  =========
read            grant grant queue
edit            grant grant queue
exclusive       queue queue queue
==============  ====  ====  =========

Two ``EDIT`` claims on the same file coexist *deliberately*.  Line-based merging
handles two agents in different functions, and where it does not, the oracle will say
so with evidence.  What the broker adds in that case is a cross-notification, so both
agents learn about each other before they duplicate or contradict work.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lumberjack.core.projections import Projections
from lumberjack.domain.claim import (
    AccessMode,
    Claim,
    DenialReason,
    Lease,
    LeaseDecision,
    LeaseDenied,
    LeaseGranted,
    LeaseQueued,
    modes_conflict,
    scopes_overlap,
)
from lumberjack.domain.events import (
    ClaimRequested,
    LeaseDeniedEvent,
    LeaseExpired,
    LeaseGrantedEvent,
    LeaseQueuedEvent,
    LeaseReleased,
)
from lumberjack.domain.glob import matches
from lumberjack.domain.workstream import ArbitrationMode, StandConfig
from lumberjack.ids import AgentId, LeaseId, WorkstreamId, new_lease_id
from lumberjack.ports.clock import Clock
from lumberjack.ports.ledger import Ledger
from lumberjack.ports.telemetry import NullTelemetry, Telemetry

__all__ = ["LeaseBroker"]


@dataclass(slots=True)
class LeaseBroker:
    ledger: Ledger
    projections: Projections
    clock: Clock
    config: StandConfig
    telemetry: Telemetry = field(default_factory=NullTelemetry)

    @property
    def _deny_on_conflict(self) -> bool:
        """``Partition`` refuses overlapping work outright; every other mode queues it."""
        return self.config.arbitration is ArbitrationMode.PARTITION

    async def request(self, claim: Claim) -> LeaseDecision:
        await self.ledger.append(ClaimRequested(claim=claim), actor=claim.claimant)
        now = self.clock.now()

        if self.projections.halted:
            return await self._deny(claim, DenialReason.STAND_HALTED)
        if claim.workstream not in self.projections.workstreams:
            return await self._deny(claim, DenialReason.WORKSTREAM_UNKNOWN)
        protected = self._protected_hit(claim)
        if protected is not None:
            return await self._deny(
                claim,
                DenialReason.PROTECTED_PATH,
                suggestion=f"{protected} is managed by the harness and cannot be claimed",
            )

        overlapping = [
            lease
            for lease in self.projections.active_leases(now)
            if lease.workstream != claim.workstream and scopes_overlap(lease.scope, claim.scope)
        ]
        blocking = [lease for lease in overlapping if modes_conflict(lease.mode, claim.mode)]

        if blocking:
            blockers = tuple(dict.fromkeys(lease.holder for lease in blocking))
            if self._deny_on_conflict:
                return await self._deny(
                    claim,
                    DenialReason.EXCLUSIVE_HELD,
                    holder=blockers[0],
                    suggestion=(
                        f"{blockers[0]} holds an overlapping lease; narrow your scope "
                        "or wait for it to land"
                    ),
                )
            return await self._queue(claim, blockers)

        coexisting = tuple(
            dict.fromkeys(
                lease.holder
                for lease in overlapping
                if lease.holder != claim.claimant and lease.mode is not AccessMode.READ
            )
        )
        return await self._grant(claim, coexisting)

    async def _grant(self, claim: Claim, coexisting: tuple[AgentId, ...]) -> LeaseGranted:
        now = self.clock.now()
        lease = Lease(
            lease_id=new_lease_id(),
            claim=claim,
            granted_at=now,
            expires_at=now + min(claim.ttl, self.config.lease_ttl),
        )
        await self.ledger.append(
            LeaseGrantedEvent(lease=lease, coexisting=coexisting), actor=claim.claimant
        )
        # `coexisting` is the interesting half: it counts the times two agents were told
        # about each other rather than made to take turns, which is the whole bet of the
        # grant matrix and, until now, a thing nobody could measure.
        self._decided(claim, "granted", coexisting=len(coexisting))
        return LeaseGranted(lease=lease, coexisting=coexisting)

    async def _queue(self, claim: Claim, blockers: tuple[AgentId, ...]) -> LeaseQueued:
        position = (
            sum(
                1 for queued in self.projections.queued if scopes_overlap(queued.scope, claim.scope)
            )
            + 1
        )
        await self.ledger.append(
            LeaseQueuedEvent(claim=claim, position=position, blockers=blockers),
            actor=claim.claimant,
        )
        self._decided(claim, "queued")
        return LeaseQueued(claim=claim, position=position, blockers=blockers)

    async def _deny(
        self,
        claim: Claim,
        reason: DenialReason,
        *,
        holder: AgentId | None = None,
        suggestion: str | None = None,
    ) -> LeaseDenied:
        await self.ledger.append(
            LeaseDeniedEvent(claim=claim, reason=reason, holder=holder), actor=claim.claimant
        )
        self._decided(claim, "denied", reason=reason.value)
        return LeaseDenied(claim=claim, reason=reason, holder=holder, suggestion=suggestion)

    def _decided(
        self, claim: Claim, outcome: str, *, coexisting: int = 0, reason: str = ""
    ) -> None:
        """``lj.lease.decision``: how often declared intent actually collides."""
        self.telemetry.counter(
            "lj.lease.decision",
            outcome=outcome,
            mode=claim.mode.value,
            coexisting=coexisting,
            reason=reason,
        )

    def _protected_hit(self, claim: Claim) -> str | None:
        from lumberjack.domain.claim import PathScope, SymbolScope

        candidates: tuple[str, ...]
        match claim.scope:
            case PathScope(patterns=patterns):
                candidates = patterns
            case SymbolScope(symbols=symbols):
                candidates = tuple(symbol.path for symbol in symbols)
        for guard in self.config.protected_paths:
            for candidate in candidates:
                if matches(guard, candidate) or candidate.startswith(
                    guard.split("*")[0].rstrip("/") + "/"
                ):
                    return candidate
        return None

    # -- lifecycle -------------------------------------------------------------------

    async def release(self, lease_id: LeaseId) -> tuple[LeaseDecision, ...]:
        lease = self.projections.leases.get(lease_id)
        if lease is None:
            return ()
        await self.ledger.append(
            LeaseReleased(lease_id=lease_id, holder=lease.holder), actor=lease.holder
        )
        return await self.drain()

    async def release_all(self, workstream: WorkstreamId) -> None:
        for lease in tuple(self.projections.leases.values()):
            if lease.workstream == workstream:
                await self.ledger.append(
                    LeaseReleased(lease_id=lease.lease_id, holder=lease.holder),
                    actor=lease.holder,
                )

    async def expire_due(self) -> tuple[LeaseDecision, ...]:
        now = self.clock.now()
        expired = [lease for lease in self.projections.leases.values() if lease.expired_at(now)]
        for lease in expired:
            await self.ledger.append(
                LeaseExpired(lease_id=lease.lease_id, holder=lease.holder), actor="system"
            )
        return await self.drain() if expired else ()

    async def drain(self) -> tuple[LeaseDecision, ...]:
        """Re-offer queued claims in arrival order once blockers are gone."""
        pending = list(self.projections.queued)
        if not pending:
            return ()
        self.projections.queued.clear()
        decisions: list[LeaseDecision] = []
        for claim in pending:
            decisions.append(await self.request(claim))
        return tuple(decisions)

    def held_by(self, workstream: WorkstreamId) -> tuple[Lease, ...]:
        return self.projections.leases_of(workstream, self.clock.now())
