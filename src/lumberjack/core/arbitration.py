"""Arbitration policies.

This module is the spec's answer to the question the whole project started from:
*should a middle-manager agent coordinate the swarm, or should the agents talk to each
other?*  Both, behind one interface, chosen per conflict.

* :class:`Partition` and :class:`FirstWriterWins` never involve a model at all.
* :class:`PeerNegotiation` gives the two agents who hold the local context a bounded
  channel to settle it themselves.
* :class:`ForemanRules` asks the manager.
* :class:`Hybrid` -- the default -- runs the peers with a turn budget and a deadline
  and lets the Foreman break the tie.  Peers hold the context that makes a good
  resolution; two models left alone will agree politely forever.  A hard budget with a
  binding tiebreaker buys the quality of the first with the termination of the second,
  and the Foreman never sees the conflicts the peers resolve on their own.
"""

from __future__ import annotations

from dataclasses import dataclass

from lumberjack.domain.accord import (
    Accord,
    ChannelState,
    Defer,
    Directive,
    EscalateToForeman,
    Ruling,
)
from lumberjack.domain.conflict import ConflictReport
from lumberjack.domain.workstream import ArbitrationMode
from lumberjack.ids import AgentId, new_accord_id
from lumberjack.ports.arbitration import ArbitrationContext, ArbitrationPolicy

__all__ = [
    "FirstWriterWins",
    "ForemanRules",
    "Hybrid",
    "Partition",
    "PeerNegotiation",
    "policy_for",
]

_SYSTEM = AgentId("system")


@dataclass(frozen=True, slots=True)
class Partition:
    """Refuse overlapping work: the later starter yields and re-scopes.

    Most of this policy's effect is upstream -- in ``PARTITION`` mode the broker denies
    an overlapping claim outright -- so by the time a conflict reaches here, the overlap
    was discovered rather than declared, and yielding is the cheapest correct answer.
    """

    name: str = "partition"

    async def arbitrate(self, report: ConflictReport, ctx: ArbitrationContext) -> Ruling:
        later = ctx.later(report)
        earlier = ctx.earlier(report)
        return Directive(
            conflict_id=report.conflict_id,
            resolution=Defer(
                yielding=ctx.agents.get(later, _SYSTEM),
                until=earlier,
                note="partition policy: overlapping scopes are not permitted to run together",
            ),
            issued_by=_SYSTEM,
            rationale=f"{later} started after {earlier} and overlaps it",
            issued_at=ctx.now,
        )


@dataclass(frozen=True, slots=True)
class FirstWriterWins:
    """Serialize: the later starter waits for the earlier to land, then auto-rebases."""

    name: str = "first_writer_wins"

    async def arbitrate(self, report: ConflictReport, ctx: ArbitrationContext) -> Ruling:
        later = ctx.later(report)
        earlier = ctx.earlier(report)
        return Directive(
            conflict_id=report.conflict_id,
            resolution=Defer(
                yielding=ctx.agents.get(later, _SYSTEM),
                until=earlier,
                note="queued behind the first writer; a rebase is scheduled on landing",
            ),
            issued_by=_SYSTEM,
            rationale=f"{earlier} holds the contested lines first",
            issued_at=ctx.now,
        )


@dataclass(frozen=True, slots=True)
class ForemanRules:
    """Hand the conflict to the manager, who issues a binding directive."""

    name: str = "foreman_rules"

    async def arbitrate(self, report: ConflictReport, ctx: ArbitrationContext) -> Ruling:
        return await ctx.ask_foreman(report, None)


@dataclass(frozen=True, slots=True)
class PeerNegotiation:
    """Let the two agents settle it, within a turn budget and a deadline."""

    name: str = "peer_negotiation"
    escalate_on_failure: bool = True

    async def arbitrate(self, report: ConflictReport, ctx: ArbitrationContext) -> Ruling:
        participants = ctx.participants(report)
        if len(participants) < 2:
            return await ctx.ask_foreman(report, None)

        channel = await ctx.open_channel(report, participants)
        channel = await ctx.run_negotiation(channel, report)

        settled = (
            channel.on_the_table is not None
            and channel.signatures == channel.participants
            and not isinstance(channel.on_the_table, EscalateToForeman)
        )
        if settled:
            return channel.settle(new_accord_id(), ctx.now)

        if not self.escalate_on_failure:
            return Directive(
                conflict_id=report.conflict_id,
                resolution=Defer(
                    yielding=ctx.agents.get(ctx.later(report), _SYSTEM),
                    until=ctx.earlier(report),
                    note="negotiation did not settle; defaulting to first writer",
                ),
                issued_by=_SYSTEM,
                rationale="peers did not reach an accord and escalation is disabled",
                issued_at=ctx.now,
            )
        return await ctx.ask_foreman(report, channel.closed(ChannelState.ESCALATED))


@dataclass(frozen=True, slots=True)
class Hybrid:
    """Peers first, Foreman as the binding tiebreaker.  The default."""

    name: str = "hybrid"
    peers: PeerNegotiation = PeerNegotiation(escalate_on_failure=True)

    async def arbitrate(self, report: ConflictReport, ctx: ArbitrationContext) -> Ruling:
        ruling = await self.peers.arbitrate(report, ctx)
        if isinstance(ruling, Accord) and isinstance(ruling.resolution, EscalateToForeman):
            return await ctx.ask_foreman(report, None)
        return ruling


def policy_for(mode: ArbitrationMode) -> ArbitrationPolicy:
    match mode:
        case ArbitrationMode.PARTITION:
            return Partition()
        case ArbitrationMode.FIRST_WRITER_WINS:
            return FirstWriterWins()
        case ArbitrationMode.PEER_NEGOTIATION:
            return PeerNegotiation(escalate_on_failure=False)
        case ArbitrationMode.FOREMAN_RULES:
            return ForemanRules()
        case ArbitrationMode.HYBRID:
            return Hybrid()
