"""The awareness digest: scoped, ordered, and hard-capped."""

from __future__ import annotations

from datetime import UTC, datetime

from lumberjack.core.digest import AwarenessDigest, render_digest
from lumberjack.core.projections import PeerActivity
from lumberjack.domain.conflict import ConflictReport, ConflictSource, Severity
from lumberjack.domain.message import Message
from lumberjack.domain.note import Note
from lumberjack.domain.workstream import DriftStatus, StandConfig
from lumberjack.ids import AgentId, ConflictId, MessageId, NoteId, TaskId, WorkstreamId

NOW = datetime(2026, 3, 1, tzinfo=UTC)
CONFIG = StandConfig()
MINE = WorkstreamId("ws-a")


def a_conflict(severity: Severity, conflict_id: str = "cfl-1") -> ConflictReport:
    return ConflictReport(
        conflict_id=ConflictId(conflict_id),
        between=(MINE, WorkstreamId("ws-b")),
        source=ConflictSource.MERGE_TREE,
        severity=severity,
        detected_at=NOW,
        evidence="CONFLICT (content): Merge conflict in pkg/core.py",
    )


def a_note(index: int) -> Note:
    return Note(
        note_id=NoteId(f"note-{index}"),
        author=AgentId("a"),
        topic="conventions",
        body=f"note body {index} " + "x" * 400,
        posted_at=NOW,
    )


def test_a_quiet_digest_says_so_briefly() -> None:
    text = render_digest(AwarenessDigest(workstream=MINE), CONFIG)

    assert "no peers overlap" in text
    assert len(text) < 200


def test_conflicts_lead_and_carry_evidence() -> None:
    digest = AwarenessDigest(workstream=MINE, conflicts=(a_conflict(Severity.BLOCK),))

    text = render_digest(digest, CONFIG)

    assert text.index("OPEN CONFLICTS") < len(text)
    assert "block" in text
    assert "Merge conflict" in text


def test_notes_are_dropped_before_conflicts_when_over_budget() -> None:
    """Context is the scarcest resource: what survives truncation is a design choice."""
    digest = AwarenessDigest(
        workstream=MINE,
        conflicts=(a_conflict(Severity.BLOCK),),
        inbox=(
            Message(
                message_id=MessageId("m1"),
                frm=AgentId("b"),
                to=AgentId("a"),
                subject="about pkg/core.py",
                body="I am renaming alpha",
                sent_at=NOW,
            ),
        ),
        notes=tuple(a_note(index) for index in range(20)),
    )
    tight = CONFIG.model_copy(update={"digest_token_cap": 200})

    text = render_digest(digest, tight)

    assert "OPEN CONFLICTS" in text
    assert "MESSAGES" in text
    assert "BLACKBOARD" not in text
    assert len(text) <= 200 * 4


def test_drift_advice_appears_only_when_it_matters() -> None:
    clean = AwarenessDigest(workstream=MINE, drift=DriftStatus(behind=1, rebase_clean=True))
    dirty = AwarenessDigest(workstream=MINE, drift=DriftStatus(behind=1, rebase_clean=False))

    assert "INTEGRATION" not in render_digest(clean, CONFIG)
    assert "will conflict" in render_digest(dirty, CONFIG)


def test_peers_are_rendered_with_their_reason() -> None:
    digest = AwarenessDigest(
        workstream=MINE,
        peers=(
            PeerActivity(
                agent=AgentId("agent-b"),
                workstream=WorkstreamId("ws-b"),
                task=TaskId("t2"),
                declared="pkg/**",
                observed_paths=("pkg/core.py",),
                mode="edit",
                rationale="adding retries",
            ),
        ),
    )

    text = render_digest(digest, CONFIG)

    assert "agent-b" in text
    assert "adding retries" in text


def test_blocking_selects_only_block_severity() -> None:
    digest = AwarenessDigest(
        workstream=MINE,
        conflicts=(a_conflict(Severity.NOTICE, "c1"), a_conflict(Severity.BLOCK, "c2")),
    )

    assert [item.conflict_id for item in digest.blocking] == ["c2"]
