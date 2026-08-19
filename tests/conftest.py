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


RECORDED_STAND = StandId("stand-recorded")
RECORDED_CONFLICT = "cfl-recorded"


@pytest.fixture
def recorded_stand(tmp_path: Path) -> tuple[Path, StandId]:
    """A stand written to disk, the way ``lj status`` and ``lj watch`` find one.

    Real events in the order a run produces them: one task that landed, one that is
    running and in a BLOCK conflict with a negotiation open on it, plus a note, a lease
    and a worktree delta, so the feed, the train, the board and the heat map all have
    something to render.  No git and no agents -- reading a ledger is all any of those
    surfaces does.
    """
    import asyncio

    repo = tmp_path / "recorded"
    (repo / ".lumberjack").mkdir(parents=True)
    asyncio.run(_write_recorded_stand(repo))
    return repo, RECORDED_STAND


async def _write_recorded_stand(repo: Path) -> None:
    from datetime import timedelta

    from lumberjack.adapters.sqlite_ledger import SqliteLedger
    from lumberjack.domain.accord import NegotiationMove
    from lumberjack.domain.claim import AccessMode, Claim, Lease, PathScope
    from lumberjack.domain.conflict import (
        ConflictedFile,
        ConflictReport,
        ConflictSource,
        Severity,
    )
    from lumberjack.domain.events import (
        ChannelOpened,
        ConflictDetected,
        LandRequested,
        LeaseGrantedEvent,
        NegotiationTurn,
        NotePosted,
        StandStarted,
        TaskAssigned,
        TaskPlanned,
        TaskStateChanged,
        WorktreeDelta,
    )
    from lumberjack.domain.note import Note
    from lumberjack.domain.task import Landed, Pending, Running, TaskSpec
    from lumberjack.domain.workstream import StandConfig, Workstream, Worktree
    from lumberjack.ids import (
        AgentId,
        ChannelId,
        CommitSha,
        ConflictId,
        LeaseId,
        NoteId,
        TaskId,
        WorkstreamId,
        repo_path,
    )

    at = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    base = CommitSha("a" * 40)
    config = StandConfig(repo=repo, base_ref="main")
    ledger = await SqliteLedger.open(
        RECORDED_STAND, repo / ".lumberjack" / RECORDED_STAND / "ledger.db"
    )
    try:
        await ledger.append(
            StandStarted(
                stand=RECORDED_STAND,
                goal="make the harness watchable",
                base=base,
                integration_branch=f"integration/{RECORDED_STAND}",
                config=config,
                pid=None,
            )
        )
        specs = {
            "task-render": TaskSpec(
                task_id=TaskId("task-render"), title="render the run", intent="render"
            ),
            "task-watch": TaskSpec(
                task_id=TaskId("task-watch"), title="watch the run", intent="watch"
            ),
        }
        workstreams: dict[str, Workstream] = {}
        for index, (name, spec) in enumerate(specs.items()):
            await ledger.append(TaskPlanned(spec=spec))
            workstream = Workstream(
                workstream_id=WorkstreamId(f"ws-{name}"),
                stand=RECORDED_STAND,
                agent=AgentId(f"agent-{name}"),
                task=spec.task_id,
                worktree=Worktree(
                    path=repo / ".lumberjack" / "worktrees" / name,
                    branch=f"lj/{RECORDED_STAND}/{name}",
                    base=base,
                ),
            )
            workstreams[name] = workstream
            await ledger.append(TaskAssigned(task=spec.task_id, workstream=workstream))
            await ledger.append(
                TaskStateChanged(
                    task_id=spec.task_id,
                    frm="pending",
                    to="assigned",
                    state=Pending(spec=spec).assign(workstream.agent, workstream.workstream_id),
                )
            )
            await ledger.append(
                WorktreeDelta(
                    workstream=workstream.workstream_id,
                    snapshot=CommitSha(f"{index}" * 40),
                    paths=(repo_path("pkg/core.py"), repo_path(f"pkg/{name}.py")),
                    lines_changed=12 + index,
                )
            )

        landed = workstreams["task-render"]
        running = workstreams["task-watch"]
        await ledger.append(
            LeaseGrantedEvent(
                lease=Lease(
                    lease_id=LeaseId("lease-recorded"),
                    claim=Claim(
                        claimant=running.agent,
                        workstream=running.workstream_id,
                        task=running.task,
                        scope=PathScope(patterns=("pkg/core.py",)),
                        mode=AccessMode.EDIT,
                        rationale="the dashboard reads it",
                    ),
                    granted_at=at,
                    expires_at=at + timedelta(days=3650),
                ),
                coexisting=(landed.agent,),
            )
        )
        report = ConflictReport(
            conflict_id=ConflictId(RECORDED_CONFLICT),
            between=(landed.workstream_id, running.workstream_id),
            source=ConflictSource.MERGE_TREE,
            severity=Severity.BLOCK,
            files=(ConflictedFile(path=repo_path("pkg/core.py"), hunks=2),),
            detected_at=at,
            evidence="<<<<<<< ours\ndef alpha() -> int:\n=======\ndef alpha() -> str:\n>>>>>>>",
        )
        await ledger.append(ConflictDetected(report=report))
        channel = ChannelId("chan-recorded")
        await ledger.append(
            ChannelOpened(
                channel_id=channel,
                conflict_id=report.conflict_id,
                participants=frozenset({landed.agent, running.agent}),
            )
        )
        await ledger.append(
            NegotiationTurn(
                channel_id=channel,
                move=NegotiationMove(by=landed.agent, say="I will take the signature", at=at),
            )
        )
        await ledger.append(
            NotePosted(
                note=Note(
                    note_id=NoteId("note-recorded"),
                    author=landed.agent,
                    topic="decisions",
                    body="alpha returns str from now on",
                    posted_at=at,
                )
            )
        )
        await ledger.append(
            LandRequested(workstream=running.workstream_id, tip=CommitSha("b" * 40), position=1)
        )
        await ledger.append(
            TaskStateChanged(
                task_id=landed.task,
                frm="awaiting_integration",
                to="landed",
                state=Landed(
                    spec=specs["task-render"],
                    agent=landed.agent,
                    workstream=landed.workstream_id,
                    tip=CommitSha("c" * 40),
                    merge=CommitSha("d" * 40),
                    landed_at=at,
                ),
            )
        )
        await ledger.append(
            TaskStateChanged(
                task_id=running.task,
                frm="assigned",
                to="running",
                state=Running(
                    spec=specs["task-watch"],
                    agent=running.agent,
                    workstream=running.workstream_id,
                    started_at=at,
                ),
            )
        )
    finally:
        await ledger.close()
