"""Frozen interface contracts and their amendment protocol.

The cure for *silent breakage*: agent A changes a signature, agent B calls it, both
worktrees are green and the merge is red.  The planner freezes the public surface at
each task boundary; breaking it requires an amendment that fans out to consumers.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lumberjack.domain.symbols import SymbolRef, signature_digest
from lumberjack.ids import AgentId, ContractId, ProposalId, TaskId

__all__ = [
    "AmendmentProposal",
    "AmendmentState",
    "Contract",
    "ContractBreach",
]


class Contract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_id: ContractId
    provider: TaskId
    consumers: frozenset[TaskId] = frozenset()
    surface: tuple[SymbolRef, ...] = Field(min_length=1)
    signature_digest: str
    frozen: bool = True

    @model_validator(mode="after")
    def _provider_is_not_consumer(self) -> Self:
        if self.provider in self.consumers:
            msg = "a task cannot consume the contract it provides"
            raise ValueError(msg)
        return self

    @classmethod
    def freeze(
        cls,
        *,
        contract_id: ContractId,
        provider: TaskId,
        consumers: frozenset[TaskId],
        surface: tuple[SymbolRef, ...],
    ) -> Contract:
        return cls(
            contract_id=contract_id,
            provider=provider,
            consumers=consumers,
            surface=surface,
            signature_digest=signature_digest(surface),
        )

    def covers(self, symbol: SymbolRef) -> bool:
        return any(item.key == symbol.key for item in self.surface)

    def breached_by(self, observed: tuple[SymbolRef, ...]) -> bool:
        """Whether the observed symbols change this contract's shape.

        Only symbols on the surface participate; a changed signature on a covered
        symbol, or the disappearance of one, is a breach.
        """
        if not self.frozen:
            return False
        seen = {symbol.key: symbol for symbol in observed}
        touched = [item for item in self.surface if item.key in seen]
        if not touched:
            return False
        rebuilt = tuple(seen[item.key] for item in touched)
        current = tuple(item for item in self.surface if item.key in seen)
        return signature_digest(rebuilt) != signature_digest(current)


class AmendmentState(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class AmendmentProposal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    proposal_id: ProposalId
    contract_id: ContractId
    proposer: AgentId
    before: str
    after: str
    migration_note: str = Field(min_length=1, max_length=2000)
    proposed_at: datetime
    state: AmendmentState = AmendmentState.PROPOSED

    def accepted(self) -> AmendmentProposal:
        return self.model_copy(update={"state": AmendmentState.ACCEPTED})

    def rejected(self) -> AmendmentProposal:
        return self.model_copy(update={"state": AmendmentState.REJECTED})


class ContractBreach(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_id: ContractId
    by: AgentId
    symbols: tuple[SymbolRef, ...]
    detected_at: datetime
