"""The intent plane: what the grant matrix does in practice."""

from __future__ import annotations

from lumberjack.core.services import Services
from lumberjack.domain.claim import (
    AccessMode,
    Claim,
    DenialReason,
    LeaseDenied,
    LeaseGranted,
    LeaseQueued,
    PathScope,
)
from lumberjack.domain.workstream import ArbitrationMode
from lumberjack.ids import AgentId, TaskId, WorkstreamId


def claim(
    workstream: WorkstreamId,
    agent: AgentId,
    task: TaskId,
    patterns: tuple[str, ...],
    mode: AccessMode,
) -> Claim:
    return Claim(
        claimant=agent,
        workstream=workstream,
        task=task,
        scope=PathScope(patterns=patterns),
        mode=mode,
        rationale="test",
    )


async def test_two_edits_on_one_file_both_get_granted(services: Services, make_workstream) -> None:
    """The single most consequential grant in the system."""
    left = await make_workstream("a")
    right = await make_workstream("b")

    first = await services.broker.request(
        claim(left.workstream_id, left.agent, left.task, ("pkg/core.py",), AccessMode.EDIT)
    )
    second = await services.broker.request(
        claim(right.workstream_id, right.agent, right.task, ("pkg/core.py",), AccessMode.EDIT)
    )

    assert isinstance(first, LeaseGranted)
    assert isinstance(second, LeaseGranted)
    assert second.coexisting == (left.agent,), "each side must learn about the other"


async def test_exclusive_queues_behind_an_edit(services: Services, make_workstream) -> None:
    left = await make_workstream("a")
    right = await make_workstream("b")
    await services.broker.request(
        claim(left.workstream_id, left.agent, left.task, ("pkg/core.py",), AccessMode.EDIT)
    )

    decision = await services.broker.request(
        claim(right.workstream_id, right.agent, right.task, ("pkg/**",), AccessMode.EXCLUSIVE)
    )

    assert isinstance(decision, LeaseQueued)
    assert decision.blockers == (left.agent,)


async def test_edit_queues_behind_an_exclusive(services: Services, make_workstream) -> None:
    left = await make_workstream("a")
    right = await make_workstream("b")
    await services.broker.request(
        claim(left.workstream_id, left.agent, left.task, ("pkg/core.py",), AccessMode.EXCLUSIVE)
    )

    decision = await services.broker.request(
        claim(right.workstream_id, right.agent, right.task, ("pkg/core.py",), AccessMode.EDIT)
    )

    assert isinstance(decision, LeaseQueued)


async def test_release_drains_the_queue(services: Services, make_workstream) -> None:
    left = await make_workstream("a")
    right = await make_workstream("b")
    granted = await services.broker.request(
        claim(left.workstream_id, left.agent, left.task, ("pkg/core.py",), AccessMode.EXCLUSIVE)
    )
    assert isinstance(granted, LeaseGranted)
    queued = await services.broker.request(
        claim(right.workstream_id, right.agent, right.task, ("pkg/core.py",), AccessMode.EDIT)
    )
    assert isinstance(queued, LeaseQueued)

    decisions = await services.broker.release(granted.lease.lease_id)

    assert any(isinstance(item, LeaseGranted) for item in decisions)


async def test_expiry_frees_a_scope(services: Services, make_workstream, clock) -> None:
    left = await make_workstream("a")
    right = await make_workstream("b")
    await services.broker.request(
        claim(left.workstream_id, left.agent, left.task, ("pkg/core.py",), AccessMode.EXCLUSIVE)
    )
    clock.advance(services.config.lease_ttl * 2)

    await services.broker.expire_due()
    decision = await services.broker.request(
        claim(right.workstream_id, right.agent, right.task, ("pkg/core.py",), AccessMode.EDIT)
    )

    assert isinstance(decision, LeaseGranted)


async def test_protected_paths_are_refused(services: Services, make_workstream) -> None:
    workstream = await make_workstream("a")

    decision = await services.broker.request(
        claim(
            workstream.workstream_id,
            workstream.agent,
            workstream.task,
            (".lumberjack/state.json",),
            AccessMode.EDIT,
        )
    )

    assert isinstance(decision, LeaseDenied)
    assert decision.reason is DenialReason.PROTECTED_PATH


async def test_unknown_workstreams_are_refused(services: Services) -> None:
    decision = await services.broker.request(
        claim(WorkstreamId("ghost"), AgentId("nobody"), TaskId("t"), ("a.py",), AccessMode.EDIT)
    )

    assert isinstance(decision, LeaseDenied)
    assert decision.reason is DenialReason.WORKSTREAM_UNKNOWN


async def test_partition_mode_denies_instead_of_queueing(
    services: Services, make_workstream
) -> None:
    left = await make_workstream("a")
    right = await make_workstream("b")
    services.broker.config = services.config.model_copy(
        update={"arbitration": ArbitrationMode.PARTITION}
    )
    await services.broker.request(
        claim(left.workstream_id, left.agent, left.task, ("pkg/core.py",), AccessMode.EXCLUSIVE)
    )

    decision = await services.broker.request(
        claim(right.workstream_id, right.agent, right.task, ("pkg/core.py",), AccessMode.EDIT)
    )

    assert isinstance(decision, LeaseDenied)
    assert decision.reason is DenialReason.EXCLUSIVE_HELD
    assert decision.suggestion is not None


async def test_disjoint_scopes_never_interact(services: Services, make_workstream) -> None:
    left = await make_workstream("a")
    right = await make_workstream("b")
    first = await services.broker.request(
        claim(left.workstream_id, left.agent, left.task, ("pkg/core.py",), AccessMode.EXCLUSIVE)
    )
    second = await services.broker.request(
        claim(
            right.workstream_id,
            right.agent,
            right.task,
            ("docs/**",),
            AccessMode.EXCLUSIVE,
        )
    )

    assert isinstance(first, LeaseGranted)
    assert isinstance(second, LeaseGranted)
    assert second.coexisting == ()
