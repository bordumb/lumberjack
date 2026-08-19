"""Human review comments: routed to whoever can act, and binding until resolved."""

from __future__ import annotations

from lumberjack.core.review import HUMAN, ReviewDesk
from lumberjack.core.services import Services
from lumberjack.core.tasks import record_transition
from lumberjack.domain.accord import Defer
from lumberjack.domain.conflict import ConflictedFile, ConflictReport, ConflictSource, Severity
from lumberjack.domain.events import ConflictDetected
from lumberjack.domain.task import Running
from lumberjack.ids import ConflictId, new_conflict_id


def desk(services: Services) -> ReviewDesk:
    return services.review


async def test_a_comment_reaches_the_agent_that_can_act(
    services: Services, make_workstream
) -> None:
    workstream = await make_workstream("a")

    comment = await desk(services).comment(
        body="this drops the error rather than reporting it",
        file="pkg/core.py",
        line_start=12,
        workstream=workstream.workstream_id,
    )

    inbox = await services.bus.inbox(workstream.agent)
    assert comment.comment_id in services.projections.comments
    assert any("review comment" in message.subject for message in inbox)


async def test_a_comment_on_a_conflict_reaches_both_sides(
    services: Services, make_workstream
) -> None:
    """A conflict is a dispute; telling one side is telling nobody."""
    left = await make_workstream("a")
    right = await make_workstream("b")
    conflict_id = new_conflict_id()
    await services.ledger.append(
        ConflictDetected(
            report=ConflictReport(
                conflict_id=conflict_id,
                between=(left.workstream_id, right.workstream_id),
                source=ConflictSource.MERGE_TREE,
                severity=Severity.BLOCK,
                files=(ConflictedFile(path="pkg/core.py"),),
                detected_at=services.clock.now(),
            )
        )
    )

    await desk(services).comment(
        body="extract this into its own module and let both import it",
        file="pkg/core.py",
        line_start=3,
        conflict_id=conflict_id,
    )

    assert await services.bus.inbox(left.agent)
    assert await services.bus.inbox(right.agent)


async def test_an_unresolved_comment_appears_in_the_digest_first(
    services: Services, make_workstream
) -> None:
    """A person outranks everything else competing for the agent's attention."""
    workstream = await make_workstream("a")
    await desk(services).comment(
        body="this needs a test before it lands",
        file="pkg/core.py",
        line_start=1,
        workstream=workstream.workstream_id,
    )

    rendered = services.digest.render(workstream.workstream_id)

    assert "HUMAN REVIEW" in rendered
    assert rendered.index("HUMAN REVIEW") < len(rendered) / 2
    assert "needs a test" in rendered


async def test_an_unresolved_comment_keeps_the_work_out_of_the_train(
    services: Services, make_workstream
) -> None:
    workstream = await make_workstream("a")
    (workstream.worktree.path / "pkg" / "core.py").write_text("def alpha() -> int:\n    return 9\n")
    tip = await services.git.commit_all(workstream.worktree, "work")
    assert tip is not None
    task = services.projections.tasks[workstream.task]
    running = task.start(services.clock.now()) if not isinstance(task, Running) else task
    await record_transition(services.ledger, services.projections, running.submit(tip))
    await services.train.request(workstream.workstream_id, tip)

    comment = await desk(services).comment(
        body="not until the docstring says what it returns",
        file="pkg/core.py",
        line_start=1,
        workstream=workstream.workstream_id,
    )
    blocked = await services.train.run_once()

    assert blocked is not None
    assert blocked.status == "bounced"
    assert "review comment" in blocked.why

    await desk(services).resolve(comment.comment_id)
    assert not desk(services).blocking(workstream.workstream_id)


async def test_a_human_ruling_is_recorded_like_the_foremans(
    services: Services, make_workstream
) -> None:
    """Binding, not advisory: the core executes it through the same path."""
    left = await make_workstream("a")
    right = await make_workstream("b")

    directive = await desk(services).rule(
        conflict_id=ConflictId("cfl-x"),
        resolution=Defer(yielding=right.agent, until=left.workstream_id),
        rationale="the telemetry work has to land first",
    )
    await services.executor.apply(directive)

    assert directive.issued_by == HUMAN
    assert services.executor.is_parked(right.workstream_id)


async def test_resolving_reopens_the_path_to_landing(services: Services, make_workstream) -> None:
    workstream = await make_workstream("a")
    comment = await desk(services).comment(
        body="rename this", file="pkg/core.py", line_start=1, workstream=workstream.workstream_id
    )
    assert desk(services).blocking(workstream.workstream_id)

    await desk(services).resolve(comment.comment_id)

    assert not desk(services).blocking(workstream.workstream_id)
    assert services.projections.comments[comment.comment_id].resolved
