"""Agent outputs.

Every agent returns a *union* of typed outcomes rather than prose, so "I am blocked"
and "this task is too big" are first-class results the supervisor can act on without
parsing anything.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lumberjack.domain.accord import Resolution
from lumberjack.domain.contract import Contract
from lumberjack.domain.task import BlockReason, TaskSpec
from lumberjack.ids import CommitSha, ContractId, RepoPath

__all__ = [
    "ForemanRuling",
    "NegotiationOutput",
    "Plan",
    "TaskBlocked",
    "TaskCompleted",
    "TaskNeedsSplit",
    "WorkerOutput",
    "WorkerReport",
]


class TaskCompleted(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["completed"] = "completed"
    summary: str = Field(min_length=1, max_length=4000)
    tip: CommitSha | None = Field(
        default=None, description="Left unset when the harness commits on the agent's behalf."
    )
    touched: tuple[RepoPath, ...] = ()
    contracts_amended: tuple[ContractId, ...] = ()


class TaskBlocked(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["blocked"] = "blocked"
    reason: BlockReason
    needs: str = Field(min_length=1, max_length=2000)


class TaskNeedsSplit(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["needs_split"] = "needs_split"
    why: str = Field(min_length=1, max_length=2000)
    proposed: tuple[TaskSpec, ...] = Field(min_length=2)


WorkerOutput = TaskCompleted | TaskBlocked | TaskNeedsSplit
"""The bare union, for generic subscription: ``Agent[WorkerDeps, WorkerOutput]``."""

WorkerReport = Annotated[WorkerOutput, Field(discriminator="kind")]
"""The discriminated form, for ``output_type=`` and for model fields."""


class Plan(BaseModel):
    """The Foreman's decomposition.  Validated as a DAG before anything runs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tasks: tuple[TaskSpec, ...] = Field(min_length=1, max_length=32)
    contracts: tuple[Contract, ...] = ()
    max_parallel: int = Field(default=4, ge=1, le=16)
    rationale: str = ""

    @model_validator(mode="after")
    def _acyclic(self) -> Plan:
        from lumberjack.domain.task import TaskGraph

        TaskGraph(tasks=self.tasks)
        return self

    def graph(self) -> object:
        from lumberjack.domain.task import TaskGraph

        return TaskGraph(tasks=self.tasks)


class ForemanRuling(BaseModel):
    """A binding decision on an escalated conflict."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    resolution: Resolution
    rationale: str = Field(min_length=1, max_length=2000)


class NegotiationOutput(BaseModel):
    """One turn a negotiating agent takes in a channel."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    say: str = Field(min_length=1, max_length=2000)
    propose: Resolution | None = None
    sign: bool = Field(
        default=False,
        description="Sign the proposal currently on the table.  Only sign what you can honour.",
    )
