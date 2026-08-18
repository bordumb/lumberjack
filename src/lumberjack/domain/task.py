"""The task lifecycle, encoded as distinct types.

There is no ``status: str`` anywhere in this module.  Each state is its own frozen
model and every transition is a method that returns the *next* type, so an illegal
transition does not typecheck.  Consumers ``match`` on :data:`Task` and close with
``assert_never``, which means adding a state breaks the build at every site that
must care.  That is the point.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from lumberjack.domain.claim import Scope
from lumberjack.domain.gate import GateReport
from lumberjack.ids import AgentId, CommitSha, ContractId, TaskId, WorkstreamId

__all__ = [
    "Abandoned",
    "Assigned",
    "AwaitingIntegration",
    "BlockReason",
    "Blocked",
    "Landed",
    "Pending",
    "Running",
    "Task",
    "TaskGraph",
    "TaskSpec",
    "task_id_of",
    "task_state",
]


class BlockReason(StrEnum):
    UNRESOLVED_CONFLICT = "unresolved_conflict"
    MISSING_DEPENDENCY = "missing_dependency"
    GATE_EXHAUSTED = "gate_exhausted"
    LEASE_DENIED = "lease_denied"
    BUDGET_EXHAUSTED = "budget_exhausted"
    AGENT_ERROR = "agent_error"
    NEEDS_HUMAN = "needs_human"


class TaskSpec(BaseModel):
    """The immutable definition of a unit of work."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: TaskId
    title: str = Field(min_length=1, max_length=200)
    intent: str = Field(min_length=1, max_length=8000)
    acceptance: tuple[str, ...] = ()
    predicted_scope: Scope | None = None
    depends_on: frozenset[TaskId] = frozenset()
    contracts: tuple[ContractId, ...] = ()


class _TaskBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    spec: TaskSpec

    @property
    def task_id(self) -> TaskId:
        return self.spec.task_id


class Pending(_TaskBase):
    kind: Literal["pending"] = "pending"

    def assign(self, agent: AgentId, workstream: WorkstreamId) -> Assigned:
        return Assigned(spec=self.spec, agent=agent, workstream=workstream)

    def abandon(self, why: str) -> Abandoned:
        return Abandoned(spec=self.spec, why=why)


class Assigned(_TaskBase):
    kind: Literal["assigned"] = "assigned"
    agent: AgentId
    workstream: WorkstreamId

    def start(self, at: datetime) -> Running:
        return Running(spec=self.spec, agent=self.agent, workstream=self.workstream, started_at=at)

    def abandon(self, why: str) -> Abandoned:
        return Abandoned(spec=self.spec, why=why)


class Running(_TaskBase):
    kind: Literal["running"] = "running"
    agent: AgentId
    workstream: WorkstreamId
    started_at: datetime
    attempts: int = Field(default=0, ge=0)
    last_gate: GateReport | None = Field(
        default=None, description="Gate failure that bounced this task, if any."
    )

    def submit(self, tip: CommitSha) -> AwaitingIntegration:
        return AwaitingIntegration(
            spec=self.spec,
            agent=self.agent,
            workstream=self.workstream,
            started_at=self.started_at,
            attempts=self.attempts,
            tip=tip,
        )

    def block(self, reason: BlockReason, detail: str = "") -> Blocked:
        return Blocked(
            spec=self.spec,
            agent=self.agent,
            workstream=self.workstream,
            reason=reason,
            detail=detail,
        )

    def abandon(self, why: str) -> Abandoned:
        return Abandoned(spec=self.spec, why=why)


class AwaitingIntegration(_TaskBase):
    kind: Literal["awaiting_integration"] = "awaiting_integration"
    agent: AgentId
    workstream: WorkstreamId
    started_at: datetime
    attempts: int = Field(default=0, ge=0)
    tip: CommitSha

    def land(self, merge: CommitSha, at: datetime) -> Landed:
        return Landed(
            spec=self.spec,
            agent=self.agent,
            workstream=self.workstream,
            tip=self.tip,
            merge=merge,
            landed_at=at,
        )

    def bounce(self, report: GateReport) -> Running:
        """Back to the same worker, with the failure as new context."""
        return Running(
            spec=self.spec,
            agent=self.agent,
            workstream=self.workstream,
            started_at=self.started_at,
            attempts=self.attempts + 1,
            last_gate=report,
        )

    def block(self, reason: BlockReason, detail: str = "") -> Blocked:
        return Blocked(
            spec=self.spec,
            agent=self.agent,
            workstream=self.workstream,
            reason=reason,
            detail=detail,
        )


class Blocked(_TaskBase):
    kind: Literal["blocked"] = "blocked"
    agent: AgentId
    workstream: WorkstreamId
    reason: BlockReason
    detail: str = ""

    def resume(self, at: datetime) -> Running:
        return Running(spec=self.spec, agent=self.agent, workstream=self.workstream, started_at=at)

    def reassign(self, agent: AgentId, workstream: WorkstreamId) -> Assigned:
        return Assigned(spec=self.spec, agent=agent, workstream=workstream)

    def abandon(self, why: str) -> Abandoned:
        return Abandoned(spec=self.spec, why=why)


class Landed(_TaskBase):
    kind: Literal["landed"] = "landed"
    agent: AgentId
    workstream: WorkstreamId
    tip: CommitSha
    merge: CommitSha
    landed_at: datetime


class Abandoned(_TaskBase):
    kind: Literal["abandoned"] = "abandoned"
    why: str = ""


Task = Annotated[
    Pending | Assigned | Running | AwaitingIntegration | Blocked | Landed | Abandoned,
    Field(discriminator="kind"),
]

TERMINAL: frozenset[str] = frozenset({"landed", "abandoned"})


def task_id_of(task: Task) -> TaskId:
    return task.spec.task_id


def task_state(task: Task) -> str:
    return task.kind


class TaskGraph(BaseModel):
    """A validated DAG of task specs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tasks: tuple[TaskSpec, ...] = Field(min_length=1)

    def model_post_init(self, _context: object) -> None:
        by_id = {spec.task_id: spec for spec in self.tasks}
        if len(by_id) != len(self.tasks):
            msg = "duplicate task ids in graph"
            raise ValueError(msg)
        for spec in self.tasks:
            unknown = spec.depends_on - by_id.keys()
            if unknown:
                msg = f"{spec.task_id} depends on unknown tasks: {sorted(unknown)}"
                raise ValueError(msg)
        self._assert_acyclic(by_id)

    @staticmethod
    def _assert_acyclic(by_id: dict[TaskId, TaskSpec]) -> None:
        white, grey, black = 0, 1, 2
        colour = dict.fromkeys(by_id, white)

        def visit(node: TaskId, trail: tuple[TaskId, ...]) -> None:
            if colour[node] == black:
                return
            if colour[node] == grey:
                cycle = " -> ".join([*trail, node])
                msg = f"task graph has a cycle: {cycle}"
                raise ValueError(msg)
            colour[node] = grey
            for parent in sorted(by_id[node].depends_on):
                visit(parent, (*trail, node))
            colour[node] = black

        for node in sorted(by_id):
            visit(node, ())

    def by_id(self) -> dict[TaskId, TaskSpec]:
        return {spec.task_id: spec for spec in self.tasks}

    def ready(self, completed: frozenset[TaskId]) -> tuple[TaskSpec, ...]:
        """Specs whose dependencies have all landed."""
        return tuple(
            spec
            for spec in self.tasks
            if spec.task_id not in completed and spec.depends_on <= completed
        )

    def topological_layers(self) -> tuple[tuple[TaskSpec, ...], ...]:
        """Specs grouped into waves that may run concurrently."""
        remaining = {spec.task_id: spec for spec in self.tasks}
        done: set[TaskId] = set()
        layers: list[tuple[TaskSpec, ...]] = []
        while remaining:
            wave = tuple(spec for spec in remaining.values() if spec.depends_on <= done)
            if not wave:  # pragma: no cover - prevented by the acyclicity check
                msg = "task graph is not schedulable"
                raise ValueError(msg)
            layers.append(wave)
            for spec in wave:
                del remaining[spec.task_id]
                done.add(spec.task_id)
        return tuple(layers)
