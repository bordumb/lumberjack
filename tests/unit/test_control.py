"""Operating a stand: liveness, and what deletion would cost."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lumberjack.core.control import stand_alive, supervisor_alive
from lumberjack.core.projections import Projections
from lumberjack.domain.events import Envelope, StandHalted, StandRenamed, TaskPlanned
from lumberjack.domain.task import TaskSpec
from lumberjack.ids import Seq, StandId, TaskId

STAND = StandId("stand-x")
NOW = datetime(2026, 3, 1, tzinfo=UTC)


def fold(*payloads: object) -> Projections:
    projections = Projections(stand=STAND)
    for index, payload in enumerate(payloads, start=1):
        projections.apply(
            Envelope(seq=Seq(index), at=NOW, stand=STAND, actor="system", payload=payload)
        )
    return projections


def test_our_own_process_is_alive() -> None:
    assert supervisor_alive(os.getpid()) is True


def test_a_dead_process_is_reported_dead() -> None:
    """The distinction a Pause button depends on: a crashed run is not a working one."""
    assert supervisor_alive(2**22) is False


def test_no_pid_is_unknown_rather_than_dead() -> None:
    assert supervisor_alive(None) is None


def test_without_a_pid_liveness_falls_back_to_the_log(tmp_path: Path) -> None:
    """Runs recorded before pids existed still deserve an evidence-based answer."""
    ledger = tmp_path / "ledger.db"
    ledger.write_bytes(b"x")

    assert stand_alive(None, ledger) is True

    stale = datetime.now(UTC) - timedelta(hours=1)
    os.utime(ledger, (stale.timestamp(), stale.timestamp()))
    assert stand_alive(None, ledger) is False


def test_a_live_process_beats_a_quiet_log(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.db"
    ledger.write_bytes(b"x")
    stale = datetime.now(UTC) - timedelta(hours=1)
    os.utime(ledger, (stale.timestamp(), stale.timestamp()))

    assert stand_alive(os.getpid(), ledger) is True


def test_lifecycle_calls_a_crashed_run_stale() -> None:
    spec = TaskSpec(task_id=TaskId("t1"), title="t", intent="i")
    projections = fold(TaskPlanned(spec=spec))

    assert projections.lifecycle(alive=True) == "live"
    assert projections.lifecycle(alive=False) == "stale"
    assert projections.lifecycle(alive=None) == "live"


def test_halting_outranks_liveness() -> None:
    spec = TaskSpec(task_id=TaskId("t1"), title="t", intent="i")
    projections = fold(TaskPlanned(spec=spec), StandHalted(reason="operator"))

    assert projections.lifecycle(alive=True) == "halted"


def test_renaming_adds_a_label_without_losing_the_goal() -> None:
    """The log is append-only; a rename cannot rewrite what was asked for."""
    projections = fold(StandRenamed(name="nightly sweep"))
    projections.goal = "implement 0002, 0003, 0004"

    assert projections.name == "nightly sweep"
    assert projections.title == "nightly sweep"
    assert projections.goal == "implement 0002, 0003, 0004"


def test_resuming_clears_the_halt_and_counts_a_session() -> None:
    """A stand is a body of work, not one process lifetime."""
    from lumberjack.domain.events import StandResumed

    spec = TaskSpec(task_id=TaskId("t1"), title="t", intent="i")
    projections = fold(
        TaskPlanned(spec=spec),
        StandHalted(reason="operator"),
        StandResumed(pid=1234, session=2, carried=(TaskId("t1"),)),
    )

    assert projections.halted is False
    assert projections.session == 2
    assert projections.pid == 1234
    assert projections.lifecycle(alive=True) == "live"


def test_outstanding_is_what_a_session_picks_up() -> None:
    from lumberjack.domain.events import TaskStateChanged
    from lumberjack.domain.task import Abandoned, Landed
    from lumberjack.ids import AgentId, CommitSha, WorkstreamId

    first = TaskSpec(task_id=TaskId("t1"), title="one", intent="i")
    second = TaskSpec(task_id=TaskId("t2"), title="two", intent="i")
    third = TaskSpec(task_id=TaskId("t3"), title="three", intent="i")
    landed = Landed(
        spec=first,
        agent=AgentId("a"),
        workstream=WorkstreamId("ws"),
        tip=CommitSha("a" * 7),
        merge=CommitSha("b" * 7),
        landed_at=NOW,
    )
    projections = fold(
        TaskPlanned(spec=first),
        TaskPlanned(spec=second),
        TaskPlanned(spec=third),
        TaskStateChanged(task_id=first.task_id, frm="running", to="landed", state=landed),
        TaskStateChanged(
            task_id=third.task_id,
            frm="pending",
            to="abandoned",
            state=Abandoned(spec=third, why="dropped"),
        ),
    )

    # Landed work is done and abandoned work was dropped; only the rest is carried.
    assert projections.outstanding() == ("t2",)
