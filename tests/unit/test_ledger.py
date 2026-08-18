"""The ledger and the projections folded over it."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from lumberjack.adapters.clock import FrozenClock
from lumberjack.adapters.memory_ledger import MemoryLedger
from lumberjack.adapters.sqlite_ledger import SqliteLedger
from lumberjack.core.projections import Projections
from lumberjack.domain.events import NotePosted, TaskPlanned
from lumberjack.domain.note import Note
from lumberjack.domain.task import TaskSpec
from lumberjack.ids import AgentId, NoteId, Seq, StandId, TaskId

STAND = StandId("stand-x")
NOW = datetime(2026, 3, 1, tzinfo=UTC)


def a_note(body: str) -> NotePosted:
    return NotePosted(
        note=Note(
            note_id=NoteId(f"note-{body}"),
            author=AgentId("a"),
            topic="decisions",
            body=body,
            posted_at=NOW,
        )
    )


def a_task(task_id: str) -> TaskPlanned:
    return TaskPlanned(spec=TaskSpec(task_id=TaskId(task_id), title=task_id, intent="do something"))


async def test_memory_ledger_assigns_a_total_order() -> None:
    ledger = MemoryLedger(stand=STAND, clock=FrozenClock(current=NOW))

    first = await ledger.append(a_task("t1"))
    second = await ledger.append(a_task("t2"))

    assert (first, second) == (1, 2)
    assert [item.kind for item in await ledger.read()] == ["task_planned"] * 2


async def test_read_filters_by_kind_and_cursor() -> None:
    ledger = MemoryLedger(stand=STAND, clock=FrozenClock(current=NOW))
    await ledger.append(a_task("t1"))
    await ledger.append(a_note("hello"))

    notes = await ledger.read(kinds=frozenset({"note_posted"}))
    after_first = await ledger.read(since=Seq(1))

    assert [item.kind for item in notes] == ["note_posted"]
    assert [item.kind for item in after_first] == ["note_posted"]


async def test_sqlite_ledger_round_trips_through_disk(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    ledger = await SqliteLedger.open(STAND, path, clock=FrozenClock(current=NOW))
    try:
        await ledger.append(a_task("t1"), actor=AgentId("agent-a"))
        await ledger.append(a_note("remember this"))
    finally:
        await ledger.close()

    reopened = await SqliteLedger.open(STAND, path)
    try:
        events = await reopened.read()
    finally:
        await reopened.close()

    assert [item.kind for item in events] == ["task_planned", "note_posted"]
    assert events[0].actor == "agent-a"
    posted = events[1].payload
    assert isinstance(posted, NotePosted)
    assert posted.note.body == "remember this"


async def test_projections_are_a_pure_fold(tmp_path: Path) -> None:
    """Replay is the whole debugging story: the same log must rebuild the same state."""
    ledger = await SqliteLedger.open(STAND, tmp_path / "ledger.db")
    try:
        for index in range(5):
            await ledger.append(a_task(f"t{index}"))
        await ledger.append(a_note("decision"))

        first = Projections(stand=STAND)
        await first.hydrate(ledger)
        second = Projections(stand=STAND)
        await second.hydrate(ledger)
    finally:
        await ledger.close()

    assert first.summary() == second.summary()
    assert set(first.specs) == set(second.specs)
    assert len(first.notes) == len(second.notes) == 1


async def test_hydrate_is_idempotent(tmp_path: Path) -> None:
    ledger = await SqliteLedger.open(STAND, tmp_path / "ledger.db")
    try:
        await ledger.append(a_note("once"))
        projections = Projections(stand=STAND)
        await projections.hydrate(ledger)
        await projections.hydrate(ledger)
    finally:
        await ledger.close()

    assert len(projections.notes) == 1


async def test_projecting_ledger_gives_read_your_own_writes() -> None:
    from lumberjack.adapters.projecting import ProjectingLedger

    projections = Projections(stand=STAND)
    ledger = ProjectingLedger(
        inner=MemoryLedger(stand=STAND, clock=FrozenClock(current=NOW)),
        projections=projections,
    )

    await ledger.append(a_task("t1"))

    assert TaskId("t1") in projections.specs
