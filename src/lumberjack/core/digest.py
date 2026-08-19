"""The awareness digest.

Awareness is scoped, not broadcast.  Context is the scarcest resource in a swarm, so
an agent is told only what intersects its own footprint, and the rendered digest is
hard-capped.  When it overflows, conflicts and the inbox win; notes are dropped first.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from lumberjack.core.projections import PeerActivity, Projections
from lumberjack.domain.conflict import ConflictReport, Severity
from lumberjack.domain.contract import Contract
from lumberjack.domain.message import Message
from lumberjack.domain.note import Note, ReviewComment
from lumberjack.domain.workstream import DriftStatus, StandConfig
from lumberjack.ids import WorkstreamId
from lumberjack.ports.clock import Clock

__all__ = ["AwarenessDigest", "DigestBuilder", "render_digest"]

_CHARS_PER_TOKEN = 4


class AwarenessDigest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    workstream: WorkstreamId
    peers: tuple[PeerActivity, ...] = ()
    conflicts: tuple[ConflictReport, ...] = ()
    notes: tuple[Note, ...] = ()
    contracts: tuple[Contract, ...] = ()
    inbox: tuple[Message, ...] = ()
    comments: tuple[ReviewComment, ...] = ()
    drift: DriftStatus = DriftStatus()
    violations: tuple[str, ...] = ()

    @property
    def blocking(self) -> tuple[ConflictReport, ...]:
        return tuple(item for item in self.conflicts if item.severity is Severity.BLOCK)

    @property
    def quiet(self) -> bool:
        return not (self.peers or self.conflicts or self.inbox or self.violations or self.comments)


@dataclass(slots=True)
class DigestBuilder:
    projections: Projections
    clock: Clock
    config: StandConfig

    def build(self, workstream: WorkstreamId) -> AwarenessDigest:
        now = self.clock.now()
        found = self.projections.workstreams.get(workstream)
        agent = found.agent if found is not None else None
        conflicts = sorted(
            self.projections.open_conflicts_for(workstream),
            key=lambda item: item.severity,
            reverse=True,
        )
        return AwarenessDigest(
            workstream=workstream,
            peers=self.projections.peers_of(workstream, now),
            conflicts=tuple(conflicts),
            notes=self.projections.notes_matching(workstream, self.config.digest_note_cap),
            contracts=(self.projections.contracts_for(found.task) if found is not None else ()),
            inbox=self.projections.unread_for(agent) if agent is not None else (),
            comments=self.projections.comments_for(workstream, unresolved_only=True),
            drift=found.drift if found is not None else DriftStatus(),
            violations=tuple(
                f"{item.violation.code}: {item.violation.detail}"
                for item in self.projections.violations[-5:]
                if item.workstream == workstream
            ),
        )

    def render(self, workstream: WorkstreamId) -> str:
        return render_digest(self.build(workstream), self.config)


def render_digest(digest: AwarenessDigest, config: StandConfig) -> str:
    """Deterministic, budget-capped rendering.  Order is the priority order."""
    if digest.quiet and digest.drift.behind == 0:
        return "Swarm awareness: no peers overlap your scope, no open conflicts, no messages."

    sections: list[tuple[str, list[str]]] = []

    if digest.comments:
        sections.append(
            (
                "HUMAN REVIEW -- unresolved, and your work cannot land until it is",
                [f"- {item.render()}" for item in digest.comments[:8]],
            )
        )
    if digest.conflicts:
        sections.append(
            (
                "OPEN CONFLICTS",
                [
                    f"- {item.summary()}"
                    + (
                        f"\n  evidence: {item.evidence.splitlines()[0][:160]}"
                        if item.evidence
                        else ""
                    )
                    for item in digest.conflicts[:6]
                ],
            )
        )
    if digest.inbox:
        sections.append(("MESSAGES", [f"- {item.render()}" for item in digest.inbox[:6]]))
    if digest.violations:
        sections.append(("PROTOCOL", [f"- {item}" for item in digest.violations]))
    if digest.peers:
        sections.append(
            ("PEERS IN YOUR SCOPE", [f"- {item.render()}" for item in digest.peers[:8]])
        )
    if digest.contracts:
        sections.append(
            (
                "FROZEN CONTRACTS",
                [
                    f"- {item.contract_id}: "
                    + ", ".join(str(symbol) for symbol in item.surface[:6])
                    for item in digest.contracts[:4]
                ],
            )
        )
    advice = digest.drift.advice(config.auto_rebase_after)
    if advice is not None:
        sections.append(("INTEGRATION", [f"- {advice}"]))
    if digest.notes:
        sections.append(("BLACKBOARD", [f"- {item.render()}" for item in digest.notes]))

    budget = config.digest_token_cap * _CHARS_PER_TOKEN
    droppable = ["BLACKBOARD", "PEERS IN YOUR SCOPE", "FROZEN CONTRACTS"]
    while _length(sections) > budget and droppable:
        victim = droppable.pop()
        sections = [item for item in sections if item[0] != victim]
    text = _assemble(sections)
    return text if len(text) <= budget else text[: budget - 3] + "..."


def _conflict_line(report: ConflictReport) -> str:
    head = f"- {report.summary()}"
    if not report.evidence:
        return head
    first = report.evidence.splitlines()[0][:160]
    return f"{head}\n  evidence: {first}"


def _length(sections: list[tuple[str, list[str]]]) -> int:
    return len(_assemble(sections))


def _assemble(sections: list[tuple[str, list[str]]]) -> str:
    blocks = ["Swarm awareness (scoped to your workstream):"]
    blocks.extend(f"{title}\n" + "\n".join(lines) for title, lines in sections)
    return "\n\n".join(blocks)
