"""The rendering layer is pure, so these assert on output rather than on a terminal.

Formatting is kept deterministic on purpose -- fixed console width, no colour codes, no
clock -- so a test that fails here is a change in what the operator is told, not a change
in how wide their window is.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Literal

import pytest
from rich.console import Console, RenderableType

from lumberjack.cli import render
from lumberjack.cli.project import GateDetection
from lumberjack.cli.render import Counters, ExitCode
from lumberjack.core.projections import Projections
from lumberjack.core.supervisor import StandOutcome, WorkstreamOutcome
from lumberjack.domain.claim import AccessMode, Claim, Lease, PathScope
from lumberjack.domain.conflict import ConflictedFile, ConflictReport, ConflictSource, Severity
from lumberjack.domain.events import (
    Bounced,
    ConflictDetected,
    Envelope,
    LeaseGrantedEvent,
    WorkstreamLanded,
    WorktreeDelta,
)
from lumberjack.domain.gate import CheckOutcome, CheckResult, GateReport
from lumberjack.domain.note import Note
from lumberjack.domain.task import Landed, TaskSpec
from lumberjack.domain.workstream import ArbitrationMode
from lumberjack.ids import (
    AgentId,
    CommitSha,
    ConflictId,
    LeaseId,
    NoteId,
    Seq,
    StandId,
    TaskId,
    WorkstreamId,
    repo_path,
)

AT = datetime(2026, 3, 1, 12, 0, 30, tzinfo=UTC)
STAND = StandId("stand-render")


def text_of(renderable: RenderableType, *, width: int = 100) -> str:
    """What the operator would see, with styling resolved away."""
    buffer = StringIO()
    Console(file=buffer, width=width, force_terminal=False, highlight=False).print(renderable)
    return buffer.getvalue()


def envelope(payload, *, seq: int = 1) -> Envelope:
    return Envelope(seq=Seq(seq), at=AT, stand=STAND, actor="system", payload=payload)


@dataclass(frozen=True, slots=True)
class Usage:
    """Stands in for 0002's ``UsageTotals``.

    Deliberately not in ``tests/fakes.py``: it exists to prove the rendering layer reads
    the published shape structurally, and inlining it here is what keeps that claim
    honest -- if 0002's real class stops satisfying the protocol, nothing here changes
    and the type checker is the one that complains.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def line_for(payload) -> render.FeedLine:
    """The feed line for an event that has to produce one."""
    line = render.feed_line(envelope(payload))
    assert line is not None, f"{payload.kind} should reach the feed"
    return line


def conflict(severity: Severity = Severity.BLOCK) -> ConflictReport:
    return ConflictReport(
        conflict_id=ConflictId("cfl-1234567890ab"),
        between=(WorkstreamId("ws-left"), WorkstreamId("ws-right")),
        source=ConflictSource.MERGE_TREE,
        severity=severity,
        files=(ConflictedFile(path=repo_path("pkg/core.py"), hunks=2),),
        detected_at=AT,
        evidence="<<<<<<< ours",
    )


# -- the feed ------------------------------------------------------------------------


def test_the_feed_says_when_a_lease_landed_next_to_a_peer():
    """A lease granted alongside someone else is the whole point of the harness."""
    granted = LeaseGrantedEvent(
        lease=Lease(
            lease_id=LeaseId("lease-1"),
            claim=Claim(
                claimant=AgentId("agent-a"),
                workstream=WorkstreamId("ws-a"),
                task=TaskId("task-a"),
                scope=PathScope(patterns=("pkg/core.py",)),
                mode=AccessMode.EDIT,
                rationale="editing alpha",
            ),
            granted_at=AT,
            expires_at=AT + timedelta(minutes=30),
        ),
        coexisting=(AgentId("agent-b"),),
    )

    line = line_for(granted)

    assert line.text == "agent-a claims pkg/core.py (edit) alongside agent-b"
    assert line.style == "yellow", "coexistence is the thing to notice, not the claim"


def test_worktree_deltas_never_reach_the_feed():
    """They fire constantly; a feed they are in is a feed nobody reads."""
    delta = WorktreeDelta(workstream=WorkstreamId("ws-a"), snapshot=CommitSha("a" * 40))

    assert render.feed_line(envelope(delta)) is None
    assert "worktree_delta" in render.COUNTED_KINDS


def test_a_block_conflict_is_red_and_a_notice_is_dim():
    assert line_for(ConflictDetected(report=conflict())).style == "bold red"
    assert line_for(ConflictDetected(report=conflict(Severity.NOTICE))).style == "dim"


def test_landing_and_bouncing_both_show_up():
    landed = WorkstreamLanded(
        workstream=WorkstreamId("ws-a"),
        task=TaskId("task-a"),
        merge=CommitSha("b" * 40),
        integration_head=CommitSha("c" * 40),
    )
    bounced = Bounced(workstream=WorkstreamId("ws-a"), task=TaskId("task-a"), why="gate failed")

    assert "landed" in line_for(landed).text
    assert "bounced" in line_for(bounced).text


def test_the_plain_form_is_one_line_with_a_timestamp():
    """`lj run > log.txt` has to stay readable; this is what a non-TTY gets."""
    plain = render.plain_feed_line(line_for(ConflictDetected(report=conflict())))

    assert plain.startswith("12:00:30  ")
    assert "\n" not in plain


def test_the_counters_carry_the_noise():
    counters = Counters(counts={}).with_event("worktree_delta").with_event("worktree_delta")

    assert "worktree_delta 2" in text_of(render.counters_line(counters))


# -- progress and exit codes ----------------------------------------------------------


def spec(task: str) -> TaskSpec:
    return TaskSpec(task_id=TaskId(task), title=f"do {task}", intent="work")


Status = Literal["completed", "partial", "halted", "failed"]


def outcome(status: Status, *, blocked: tuple[str, ...] = ()) -> StandOutcome:
    return StandOutcome(
        status=status,
        goal="a goal",
        integration_branch="integration/stand-render",
        landed=(TaskId("task-a"),),
        blocked=tuple(TaskId(item) for item in blocked),
        workstreams=(
            WorkstreamOutcome(
                workstream=WorkstreamId("ws-a"),
                agent=AgentId("agent-a"),
                task=Landed(
                    spec=spec("task-a"),
                    agent=AgentId("agent-a"),
                    workstream=WorkstreamId("ws-a"),
                    tip=CommitSha("a" * 40),
                    merge=CommitSha("b" * 40),
                    landed_at=AT,
                ),
                branch="lj/stand-render/task-a",
            ),
        ),
    )


def test_a_completed_run_exits_zero():
    assert render.exit_code_for(outcome("completed")) is ExitCode.OK


@pytest.mark.parametrize(
    ("status", "blocked"),
    [("partial", ()), ("halted", ()), ("failed", ()), ("completed", ("task-b",))],
)
def test_anything_short_of_completed_exits_non_zero(status, blocked):
    """A run that blocked a task and exits zero cannot be used in CI."""
    assert render.exit_code_for(outcome(status, blocked=blocked)) is ExitCode.PARTIAL


def test_the_outcome_reports_tokens_when_there_are_any():
    body = text_of(
        render.outcome_report(
            outcome("completed"), usage=Usage(input_tokens=1200, output_tokens=340, requests=7)
        )
    )

    assert "1,540" in body
    assert "7 request(s)" in body


def test_the_outcome_says_nothing_about_tokens_when_usage_is_not_wired_up():
    """0002 owns the usage ledger; an empty panel beats blocking on a sibling."""
    assert "tokens" not in text_of(render.outcome_report(outcome("completed")))


# -- status ---------------------------------------------------------------------------


def hydrated(repo: Path, stand: StandId) -> Projections:
    import asyncio

    from lumberjack.adapters.sqlite_ledger import SqliteLedger

    async def go() -> Projections:
        ledger = await SqliteLedger.open(stand, repo / ".lumberjack" / stand / "ledger.db")
        projections = Projections(stand=stand)
        try:
            await projections.hydrate(ledger)
        finally:
            await ledger.close()
        return projections

    return asyncio.run(go())


def test_status_says_whether_anything_landed(recorded_stand):
    """An integration head on its own is a number nobody can act on."""
    repo, stand = recorded_stand
    projections = hydrated(repo, stand)

    body = text_of(
        render.status_view(projections, stand=stand, lifecycle="live", now=AT), width=120
    )

    assert "1 task(s) landed" in body


def test_status_json_keeps_identifiers_whole(recorded_stand):
    repo, stand = recorded_stand
    projections = hydrated(repo, stand)

    document = render.status_json(projections, stand=stand, lifecycle="live", now=AT)

    assert document["conflicts"][0]["conflict"] == "cfl-recorded"
    assert [item["workstream"] for item in document["workstreams"]] == [
        "ws-task-render",
        "ws-task-watch",
    ]
    assert document["integration"]["landed"] == 1


def test_a_table_truncates_an_id_but_the_document_does_not(recorded_stand):
    repo, stand = recorded_stand
    projections = hydrated(repo, stand)

    table = text_of(render.conflicts_view(projections), width=120)
    document = render.conflicts_json(projections)

    assert "cfl-reco " in table, "ids are truncated to eight characters in tables"
    assert document[0]["conflict"] == "cfl-recorded"


# -- the conflict detail --------------------------------------------------------------


def test_the_conflict_detail_carries_evidence_hunks_and_transcript(recorded_stand):
    """The single most informative thing the system knows about a pair of agents."""
    repo, stand = recorded_stand
    projections = hydrated(repo, stand)
    report = projections.conflicts[ConflictId("cfl-recorded")]

    body = text_of(render.conflict_detail(report, projections.channels.values()), width=120)

    assert "pkg/core.py" in body
    assert "def alpha() -> str:" in body, "the oracle's evidence"
    assert "I will take the signature" in body, "the negotiation so far"


# -- the train and the heat map -------------------------------------------------------


def test_the_train_shows_the_queue_position_and_the_gate_stage(recorded_stand):
    repo, stand = recorded_stand
    projections = hydrated(repo, stand)
    projections.gate_reports[WorkstreamId("ws-task-watch")] = _gate_run()

    entries = render.train_entries(projections)

    assert [entry.position for entry in entries] == [1]
    assert entries[0].stage == "gate: pytest failed"


def _gate_run():
    from lumberjack.domain.events import GateRun

    return GateRun(
        workstream=WorkstreamId("ws-task-watch"),
        report=GateReport(
            checks=(
                CheckResult(name="ruff", command=("ruff",), outcome=CheckOutcome.PASSED),
                CheckResult(
                    name="pytest", command=("pytest",), outcome=CheckOutcome.FAILED, exit_code=1
                ),
            )
        ),
    )


def test_the_heat_map_ranks_the_file_the_most_workstreams_touch(recorded_stand):
    """The view that tells you the task decomposition was wrong."""
    repo, stand = recorded_stand
    projections = hydrated(repo, stand)

    hottest, count = render.heat_map(projections)[0]

    assert hottest == "pkg/core.py"
    assert count == 2


# -- the small sharp edges -------------------------------------------------------------


def test_no_stands_says_what_to_do_next():
    body = text_of(render.no_stands(Path("/repo"), action="status"))

    assert "lj run" in body
    assert "--stand" in body


def test_an_empty_gate_explains_what_to_fill_in():
    """``lumberjack.json`` is strict JSON, so the comment has to be on the screen."""
    detection = GateDetection(commands=(), project="unknown", why="nothing to infer from")

    body = text_of(
        render.init_report(
            config_path=Path("/repo/lumberjack.json"),
            state_root=Path("/repo/.lumberjack"),
            detection=detection,
            arbitration=ArbitrationMode.HYBRID,
            max_parallel=4,
        ),
        width=120,
    )

    assert "gate_commands" in body
    assert "nothing is checked" in body


def test_the_board_renders_notes():
    note = Note(
        note_id=NoteId("note-1"),
        author=AgentId("agent-a"),
        topic="decisions",
        body="alpha returns str",
        posted_at=AT,
    )

    assert "alpha returns str" in text_of(render.board_view([note]), width=120)
    assert render.board_json([note])[0]["note"] == "note-1"


# -- the house rule --------------------------------------------------------------------


def test_no_bare_print_survives_anywhere_in_the_package():
    """``print`` belongs to ``cli/``, and inside it to the rendering layer alone.

    Walking the AST rather than grepping so that a ``print`` hidden in a docstring or a
    string literal does not fail the build, and a real one cannot hide behind a comment.
    """
    package = Path(__file__).resolve().parents[2] / "src" / "lumberjack"
    offenders = [
        f"{path.relative_to(package)}:{node.lineno}"
        for path in package.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "print"
    ]

    assert offenders == [], "route it through lumberjack.cli.output.Output"
