"""A finished stand must not read as a live one.

The ledger is history, and every view over it is a fold. That makes it very easy to
render a stand that stopped hours ago exactly like one that is working right now --
which is what happened, and what these tests exist to prevent.
"""

from __future__ import annotations

from datetime import UTC, datetime

from lumberjack.core.projections import Projections
from lumberjack.domain.events import Envelope, StandHalted, TaskPlanned, TaskStateChanged
from lumberjack.domain.task import Blocked, Landed, Pending, TaskSpec
from lumberjack.ids import AgentId, CommitSha, Seq, StandId, TaskId, WorkstreamId

STAND = StandId("stand-x")
NOW = datetime(2026, 3, 1, tzinfo=UTC)


def fold(projections: Projections, *payloads: object) -> Projections:
    for index, payload in enumerate(payloads, start=1):
        projections.apply(
            Envelope(seq=Seq(index), at=NOW, stand=STAND, actor="system", payload=payload)
        )
    return projections


def a_spec(task_id: str) -> TaskSpec:
    return TaskSpec(task_id=TaskId(task_id), title=task_id, intent="do it")


def landed(spec: TaskSpec) -> TaskStateChanged:
    state = Landed(
        spec=spec,
        agent=AgentId("a"),
        workstream=WorkstreamId("ws"),
        tip=CommitSha("a" * 7),
        merge=CommitSha("b" * 7),
        landed_at=NOW,
    )
    return TaskStateChanged(task_id=spec.task_id, frm="running", to="landed", state=state)


def blocked(spec: TaskSpec) -> TaskStateChanged:
    state = Blocked(
        spec=spec,
        agent=AgentId("a"),
        workstream=WorkstreamId("ws"),
        reason="needs_human",
        detail="",
    )
    return TaskStateChanged(task_id=spec.task_id, frm="running", to="blocked", state=state)


def test_an_empty_stand_is_not_finished() -> None:
    assert Projections(stand=STAND).lifecycle() == "live"


def test_a_stand_with_work_outstanding_is_live() -> None:
    spec = a_spec("t1")
    projections = fold(Projections(stand=STAND), TaskPlanned(spec=spec))

    assert isinstance(projections.tasks[spec.task_id], Pending)
    assert projections.lifecycle() == "live"


def test_every_task_terminal_means_finished() -> None:
    first, second = a_spec("t1"), a_spec("t2")
    projections = fold(
        Projections(stand=STAND),
        TaskPlanned(spec=first),
        TaskPlanned(spec=second),
        landed(first),
        blocked(second),
    )

    assert projections.finished()
    assert projections.lifecycle() == "finished"


def test_one_task_still_running_keeps_the_stand_live() -> None:
    first, second = a_spec("t1"), a_spec("t2")
    projections = fold(
        Projections(stand=STAND),
        TaskPlanned(spec=first),
        TaskPlanned(spec=second),
        landed(first),
    )

    assert not projections.finished()
    assert projections.lifecycle() == "live"


def test_halting_wins_over_outstanding_work() -> None:
    """`lj halt` stops the stand whether or not the tasks agree."""
    projections = fold(
        Projections(stand=STAND),
        TaskPlanned(spec=a_spec("t1")),
        StandHalted(reason="operator halt"),
    )

    assert projections.lifecycle() == "halted"
    assert projections.finished()
