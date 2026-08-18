"""The merge train: oracle pre-check, gate, merge, bounce."""

from __future__ import annotations

from lumberjack.adapters.uv_gate import CommandGate
from lumberjack.core.services import Services
from lumberjack.core.tasks import record_transition
from lumberjack.domain.task import (
    Assigned,
    AwaitingIntegration,
    Blocked,
    Landed,
    Running,
)
from lumberjack.domain.workstream import Workstream


def body(alpha: int = 1, gamma: int = 3) -> str:
    return (
        f"def alpha() -> int:\n    return {alpha}\n\n\n"
        "def beta() -> int:\n    return 2\n\n\n"
        f"def gamma() -> int:\n    return {gamma}\n"
    )


async def submit(services: Services, workstream: Workstream, content: str) -> None:
    (workstream.worktree.path / "pkg" / "core.py").write_text(content)
    tip = await services.git.commit_all(workstream.worktree, "work")
    assert tip is not None
    task = services.projections.tasks[workstream.task]
    assert isinstance(task, Assigned | Running)
    running = task if isinstance(task, Running) else task.start(services.clock.now())
    await record_transition(services.ledger, services.projections, running.submit(tip))
    await services.train.request(workstream.workstream_id, tip)


async def test_a_clean_workstream_lands(services: Services, make_workstream) -> None:
    workstream = await make_workstream("a")
    await submit(services, workstream, body(alpha=111))

    outcome = await services.train.run_once()

    assert outcome is not None
    assert outcome.status == "landed"
    assert isinstance(services.projections.tasks[workstream.task], Landed)
    assert services.projections.integration_head == outcome.merge


async def test_the_train_serializes_two_compatible_workstreams(
    services: Services, make_workstream
) -> None:
    left = await make_workstream("a")
    right = await make_workstream("b")
    await submit(services, left, body(alpha=111))
    await submit(services, right, body(gamma=333))

    outcomes = await services.train.drain()

    assert [item.status for item in outcomes] == ["landed", "landed"]
    head = services.projections.integration_head
    assert head is not None
    merged = (await services.git.read_blob(head, "pkg/core.py") or b"").decode()
    assert "return 111" in merged
    assert "return 333" in merged


async def test_a_conflicting_second_entry_bounces_before_the_gate(
    services: Services, make_workstream
) -> None:
    """Never burn a test run on a merge git has already refused."""
    left = await make_workstream("a")
    right = await make_workstream("b")
    await submit(services, left, body(alpha=111))
    await submit(services, right, body(alpha=999))

    outcomes = await services.train.drain()

    assert outcomes[0].status == "landed"
    assert outcomes[1].status == "bounced"
    bounced = services.projections.tasks[right.task]
    assert isinstance(bounced, Running)
    assert bounced.attempts == 1


async def test_a_failing_gate_bounces_with_the_log(services: Services, make_workstream) -> None:
    services.train.gate = CommandGate(commands=(("false",),), fail_fast=True)
    workstream = await make_workstream("a")
    await submit(services, workstream, body(alpha=111))

    outcome = await services.train.run_once()

    assert outcome is not None
    assert outcome.status == "bounced"
    assert outcome.report is not None
    assert not outcome.report.passed


async def test_the_bounce_limit_blocks_the_task(services: Services, make_workstream) -> None:
    services.train.gate = CommandGate(commands=(("false",),))
    services.train.config = services.config.model_copy(update={"bounce_limit": 1})
    workstream = await make_workstream("a")
    await submit(services, workstream, body(alpha=111))

    first = await services.train.run_once()
    assert first is not None and first.status == "bounced"

    task = services.projections.tasks[workstream.task]
    assert isinstance(task, Running)
    await record_transition(
        services.ledger, services.projections, task.submit(workstream.worktree.base)
    )
    await services.train.request(workstream.workstream_id, workstream.worktree.base)
    second = await services.train.run_once()

    assert second is not None
    assert second.status == "blocked"
    assert isinstance(services.projections.tasks[workstream.task], Blocked)


async def test_requesting_a_land_records_a_position(services: Services, make_workstream) -> None:
    left = await make_workstream("a")
    right = await make_workstream("b")
    await submit(services, left, body(alpha=111))
    await submit(services, right, body(gamma=333))

    assert services.train.queue == [left.workstream_id, right.workstream_id]
    task = services.projections.tasks[right.task]
    assert isinstance(task, AwaitingIntegration)
