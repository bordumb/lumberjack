"""Conflict resolution: peer accords, foreman directives, and the negotiation channel.

The spec's answer to "middle manager or peer chat?" lives here as data: both produce a
:data:`Ruling`, and the core executes rulings rather than trusting agents to honour them.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from itertools import combinations
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lumberjack.domain.claim import Scope, scopes_overlap
from lumberjack.domain.symbols import SymbolRef
from lumberjack.ids import (
    AccordId,
    AgentId,
    ChannelId,
    ConflictId,
    RepoPath,
    WorkstreamId,
)

__all__ = [
    "Accord",
    "Adopt",
    "Channel",
    "ChannelState",
    "Defer",
    "Directive",
    "EscalateToForeman",
    "Extract",
    "NegotiationLimits",
    "NegotiationMove",
    "Resolution",
    "Ruling",
    "Split",
    "ruling_resolution",
]


class Defer(BaseModel):
    """One side waits for the other to land, then rebases."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["defer"] = "defer"
    yielding: AgentId
    until: WorkstreamId
    note: str = ""


class Split(BaseModel):
    """Carve the contested scope into disjoint pieces."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["split"] = "split"
    assignments: tuple[tuple[AgentId, Scope], ...] = Field(min_length=2)

    @model_validator(mode="after")
    def _disjoint(self) -> Self:
        agents = [agent for agent, _ in self.assignments]
        if len(set(agents)) != len(agents):
            msg = "each agent may appear at most once in a split"
            raise ValueError(msg)
        for (left_agent, left), (right_agent, right) in combinations(self.assignments, 2):
            if scopes_overlap(left, right):
                msg = (
                    "split assignments must be disjoint, but "
                    f"{left_agent} and {right_agent} still overlap"
                )
                raise ValueError(msg)
        return self


class Extract(BaseModel):
    """Move the contested code into a new module with a single owner."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["extract"] = "extract"
    new_module: RepoPath
    owner: AgentId
    moved: tuple[SymbolRef, ...] = Field(min_length=1)


class Adopt(BaseModel):
    """One implementation becomes canonical; the other side imports it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["adopt"] = "adopt"
    canonical: AgentId
    superseded: AgentId
    note: str = ""

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        if self.canonical == self.superseded:
            msg = "adopt requires two distinct agents"
            raise ValueError(msg)
        return self


class EscalateToForeman(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["escalate"] = "escalate"
    reason: str = Field(min_length=1, max_length=1000)


Resolution = Annotated[
    Defer | Split | Extract | Adopt | EscalateToForeman, Field(discriminator="kind")
]


class Accord(BaseModel):
    """A resolution signed by every participant.  An unsigned accord is unrepresentable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["accord"] = "accord"
    accord_id: AccordId
    conflict_id: ConflictId
    resolution: Resolution
    signed_by: frozenset[AgentId] = Field(min_length=1)
    participants: frozenset[AgentId] = Field(min_length=1)
    agreed_at: datetime

    @model_validator(mode="after")
    def _fully_signed(self) -> Self:
        if self.signed_by != self.participants:
            missing = sorted(self.participants - self.signed_by)
            extra = sorted(self.signed_by - self.participants)
            msg = f"accord is not fully signed (missing={missing}, unexpected={extra})"
            raise ValueError(msg)
        return self


class Directive(BaseModel):
    """A binding ruling from the Foreman.  Issued on deadlock or budget exhaustion."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["directive"] = "directive"
    conflict_id: ConflictId
    resolution: Resolution
    issued_by: AgentId
    rationale: str = Field(min_length=1, max_length=2000)
    issued_at: datetime


Ruling = Annotated[Accord | Directive, Field(discriminator="kind")]


def ruling_resolution(ruling: Ruling) -> Resolution:
    return ruling.resolution


class NegotiationLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    turns: int = Field(default=6, ge=2, le=32)
    deadline: timedelta = timedelta(minutes=5)


class ChannelState(StrEnum):
    OPEN = "open"
    SETTLED = "settled"
    ESCALATED = "escalated"
    EXPIRED = "expired"


class NegotiationMove(BaseModel):
    """One turn in a negotiation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    by: AgentId
    say: str = Field(min_length=1, max_length=2000)
    propose: Resolution | None = None
    sign: bool = Field(default=False, description="Sign the proposal currently on the table.")
    at: datetime


class Channel(BaseModel):
    """A bounded, ledger-backed negotiation between the participants of one conflict."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    channel_id: ChannelId
    conflict_id: ConflictId
    participants: frozenset[AgentId] = Field(min_length=2)
    limits: NegotiationLimits = NegotiationLimits()
    opened_at: datetime
    moves: tuple[NegotiationMove, ...] = ()
    state: ChannelState = ChannelState.OPEN

    @property
    def turns_used(self) -> int:
        return len(self.moves)

    @property
    def turns_left(self) -> int:
        return max(0, self.limits.turns - self.turns_used)

    @property
    def on_the_table(self) -> Resolution | None:
        for move in reversed(self.moves):
            if move.propose is not None:
                return move.propose
        return None

    @property
    def signatures(self) -> frozenset[AgentId]:
        """Signatures collected since the current proposal was tabled."""
        collected: set[AgentId] = set()
        for move in reversed(self.moves):
            if move.sign:
                collected.add(move.by)
            if move.propose is not None:
                break
        return frozenset(collected)

    def whose_turn(self) -> AgentId:
        order = sorted(self.participants)
        return order[self.turns_used % len(order)]

    def expired_at(self, now: datetime) -> bool:
        return now - self.opened_at >= self.limits.deadline

    def exhausted(self, now: datetime) -> bool:
        return self.turns_left <= 0 or self.expired_at(now)

    def with_move(self, move: NegotiationMove) -> Channel:
        if self.state is not ChannelState.OPEN:
            msg = f"channel {self.channel_id} is {self.state.value}"
            raise ValueError(msg)
        if move.by not in self.participants:
            msg = f"{move.by} is not a participant in {self.channel_id}"
            raise ValueError(msg)
        return self.model_copy(update={"moves": (*self.moves, move)})

    def closed(self, state: ChannelState) -> Channel:
        return self.model_copy(update={"state": state})

    def settle(self, accord_id: AccordId, now: datetime) -> Accord:
        """Produce the signed accord, or fail loudly if it is not actually agreed."""
        proposal = self.on_the_table
        if proposal is None:
            msg = "nothing has been proposed in this channel"
            raise ValueError(msg)
        return Accord(
            accord_id=accord_id,
            conflict_id=self.conflict_id,
            resolution=proposal,
            signed_by=self.signatures,
            participants=self.participants,
            agreed_at=now,
        )

    def transcript(self) -> str:
        return "\n".join(
            f"{move.by}: {move.say}"
            + (f" [proposes {move.propose.kind}]" if move.propose else "")
            + (" [signs]" if move.sign else "")
            for move in self.moves
        )
