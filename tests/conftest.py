"""Shared fixtures: a real git repository, and a fully wired stand over it."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lumberjack.adapters.ast_indexer import AstIndexer
from lumberjack.adapters.clock import FrozenClock
from lumberjack.adapters.git_cli import GitCli
from lumberjack.adapters.memory_ledger import MemoryLedger
from lumberjack.adapters.projecting import ProjectingLedger
from lumberjack.adapters.uv_gate import NullGate
from lumberjack.core.projections import Projections
from lumberjack.core.services import Services
from lumberjack.domain.workstream import StandConfig
from lumberjack.ids import StandId

GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}

BASE_FILE = """\
def alpha() -> int:
    return 1


def beta() -> int:
    return 2


def gamma() -> int:
    return 3
"""

CALLER_FILE = """\
from pkg.core import alpha


def use() -> int:
    return alpha()
"""


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        capture_output=True,
        env={**GIT_ENV, "PATH": "/usr/bin:/bin:/usr/local/bin"},
        check=False,
    )
    if result.returncode:
        msg = f"git {' '.join(args)} failed: {result.stderr.decode()}"
        raise RuntimeError(msg)
    return result.stdout.decode()


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[Path]:
    """A small python repository with a caller, so blast radius has something to find."""
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    git(root, "init", "-b", "main")
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "core.py").write_text(BASE_FILE)
    (root / "pkg" / "caller.py").write_text(CALLER_FILE)
    (root / "README.md").write_text("# fixture\n")
    git(root, "add", "-A")
    git(root, "commit", "-m", "base")
    yield root


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(current=datetime(2026, 3, 1, 12, 0, tzinfo=UTC))


@pytest.fixture
def config(repo: Path) -> StandConfig:
    return StandConfig(repo=repo, max_parallel=4, base_ref="main")


@pytest.fixture
def stand_id() -> StandId:
    return StandId("stand-test")


@pytest.fixture
def services(repo: Path, config: StandConfig, clock: FrozenClock, stand_id: StandId) -> Services:
    projections = Projections(stand=stand_id)
    ledger = ProjectingLedger(
        inner=MemoryLedger(stand=stand_id, clock=clock), projections=projections
    )
    return Services.wire(
        stand=stand_id,
        config=config,
        clock=clock,
        git=GitCli(repo=repo),
        ledger=ledger,
        indexer=AstIndexer(package_roots=("",)),
        gate=NullGate(),
        projections=projections,
    )


@pytest.fixture
def make_workstream(services: Services):
    """Create a real worktree and register it as an active workstream."""
    from lumberjack.core.tasks import record_transition
    from lumberjack.domain.events import StandStarted, TaskAssigned, TaskPlanned
    from lumberjack.domain.task import Pending, TaskSpec
    from lumberjack.domain.workstream import Workstream
    from lumberjack.ids import AgentId, TaskId, WorkstreamId

    async def build(name: str, *, title: str = "work") -> Workstream:
        base = await services.git.resolve(services.config.base_ref)
        if not services.projections.integration_branch:
            branch = services.config.integration_branch(services.stand)
            await services.git.create_branch(branch, base, force=True)
            await services.ledger.append(
                StandStarted(
                    stand=services.stand,
                    goal="fixture",
                    base=base,
                    integration_branch=branch,
                    config=services.config,
                )
            )
        task_id = TaskId(f"task-{name}")
        spec = TaskSpec(task_id=task_id, title=title, intent=title)
        await services.ledger.append(TaskPlanned(spec=spec))
        worktree = await services.git.add_worktree(
            f"lj/{name}", base, services.config.resolved_worktree_root() / name
        )
        workstream = Workstream(
            workstream_id=WorkstreamId(f"ws-{name}"),
            stand=services.stand,
            agent=AgentId(f"agent-{name}"),
            task=task_id,
            worktree=worktree,
        )
        await services.ledger.append(TaskAssigned(task=task_id, workstream=workstream))
        await record_transition(
            services.ledger,
            services.projections,
            Pending(spec=spec).assign(workstream.agent, workstream.workstream_id),
        )
        services.projections.integration_head = base
        return workstream

    return build
