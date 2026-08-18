"""The public API surface, and its promise never to destroy unlanded work."""

from __future__ import annotations

from pathlib import Path

from lumberjack.adapters.ast_indexer import AstIndexer
from lumberjack.adapters.clock import FrozenClock
from lumberjack.adapters.git_cli import GitCli
from lumberjack.adapters.memory_ledger import MemoryLedger
from lumberjack.adapters.uv_gate import NullGate
from lumberjack.domain.workstream import StandConfig
from lumberjack.ids import StandId
from lumberjack.stand import Stand


async def open_stand(repo: Path, clock: FrozenClock) -> Stand:
    return await Stand.create(
        StandConfig(repo=repo, base_ref="main"),
        stand_id=StandId("stand-api"),
        git=GitCli(repo=repo),
        ledger=MemoryLedger(stand=StandId("stand-api"), clock=clock),
        indexer=AstIndexer(package_roots=("",)),
        gate=NullGate(),
        clock=clock,
    )


async def test_prepare_creates_the_integration_branch(repo: Path, clock: FrozenClock) -> None:
    stand = await open_stand(repo, clock)
    try:
        base = await stand.supervisor.prepare("a goal")
    finally:
        await stand.close()

    branch = stand.config.integration_branch(stand.stand_id)
    assert await stand.services.git.resolve(branch) == base
    assert stand.services.projections.goal == "a goal"


async def test_close_preserves_worktrees_holding_unlanded_work(
    repo: Path, clock: FrozenClock, make_workstream
) -> None:
    """A crash must never cost an agent its output."""
    stand = await open_stand(repo, clock)
    await stand.supervisor.prepare("a goal")
    base = await stand.services.git.resolve("main")
    worktree = await stand.services.git.add_worktree(
        "lj/keepme", base, stand.config.resolved_worktree_root() / "keepme"
    )
    from lumberjack.domain.events import TaskAssigned
    from lumberjack.domain.task import TaskSpec
    from lumberjack.domain.workstream import Workstream
    from lumberjack.ids import AgentId, TaskId, WorkstreamId

    spec = TaskSpec(task_id=TaskId("task-keep"), title="keep", intent="keep")
    await stand.services.ledger.append(
        TaskAssigned(
            task=spec.task_id,
            workstream=Workstream(
                workstream_id=WorkstreamId("ws-keep"),
                stand=stand.stand_id,
                agent=AgentId("agent-keep"),
                task=spec.task_id,
                worktree=worktree,
            ),
        )
    )

    await stand.close()

    assert str(worktree.path) in stand.preserved
    assert worktree.path.is_dir(), "the worktree must still be on disk"


async def test_open_is_an_async_context_manager(repo: Path, clock: FrozenClock) -> None:
    config = StandConfig(repo=repo, base_ref="main")
    async with Stand.open(
        config,
        stand_id=StandId("stand-ctx"),
        git=GitCli(repo=repo),
        ledger=MemoryLedger(stand=StandId("stand-ctx"), clock=clock),
        indexer=AstIndexer(package_roots=("",)),
        gate=NullGate(),
        clock=clock,
    ) as stand:
        assert stand.stand_id == "stand-ctx"
        assert stand.state_dir.name == "stand-ctx"
