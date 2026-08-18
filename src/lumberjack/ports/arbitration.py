"""Arbitration: the interface behind which "manager rules" and "peers negotiate" are
merely two implementations.

This is the spec's answer to the open design question.  ``Partition`` and
``FirstWriterWins`` never involve a model at all; ``PeerNegotiation`` runs a bounded
channel between the two agents; ``ForemanRules`` asks the manager; ``Hybrid`` is
peer negotiation with the manager as a binding tiebreaker on deadlock or timeout.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from lumberjack.domain.accord import Channel, Directive, NegotiationLimits, Ruling
from lumberjack.domain.claim import Claim
from lumberjack.domain.conflict import ConflictReport
from lumberjack.ids import AgentId, StandId, WorkstreamId

__all__ = ["ArbitrationContext", "ArbitrationPolicy"]


@dataclass(frozen=True, slots=True)
class ArbitrationContext:
    """Everything a policy may use, injected so policies stay pure of transport."""

    stand: StandId
    now: datetime
    agents: dict[WorkstreamId, AgentId]
    claims: dict[WorkstreamId, tuple[Claim, ...]]
    started: dict[WorkstreamId, datetime]
    limits: NegotiationLimits
    open_channel: Callable[[ConflictReport, frozenset[AgentId]], Awaitable[Channel]]
    run_negotiation: Callable[[Channel, ConflictReport], Awaitable[Channel]]
    ask_foreman: Callable[[ConflictReport, Channel | None], Awaitable[Directive]]

    def earlier(self, report: ConflictReport) -> WorkstreamId:
        """Whichever participant started work first.  The tiebreak is deterministic."""
        left, right = report.between
        far_future = datetime.max.replace(tzinfo=UTC)
        return min(
            (left, right),
            key=lambda item: (self.started.get(item, far_future), item),
        )

    def later(self, report: ConflictReport) -> WorkstreamId:
        return report.other(self.earlier(report))

    def participants(self, report: ConflictReport) -> frozenset[AgentId]:
        return frozenset(
            self.agents[workstream] for workstream in report.between if workstream in self.agents
        )


class ArbitrationPolicy(Protocol):
    @property
    def name(self) -> str: ...

    async def arbitrate(self, report: ConflictReport, ctx: ArbitrationContext) -> Ruling: ...
