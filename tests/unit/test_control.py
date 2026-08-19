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
