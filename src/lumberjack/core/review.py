"""Human review comments, routed into the swarm.

The point of a comment is not to sit on a screen where a person can find it later. It
is to reach the agent that is about to write the next line in that file, which is why
it lands in the awareness digest and why an unresolved one holds up the merge train.

Where it goes depends on where it was left:

* on a workstream -- to that agent, because it is the one that can act;
* on a conflict -- to the foreman, because the conflict is a dispute and the foreman is
  the mediator. The foreman is not a running process, so the comment *invokes* an
  arbitration rather than landing in an inbox nobody is reading.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from lumberjack.core.board import LedgerMessageBus
from lumberjack.core.projections import Projections
from lumberjack.domain.accord import Directive, EscalateToForeman, Resolution
from lumberjack.domain.events import (
    DirectiveIssued,
    ReviewCommentPosted,
    ReviewCommentResolved,
)
from lumberjack.domain.note import ReviewComment
from lumberjack.ids import (
    AgentId,
    CommentId,
    ConflictId,
    RepoPath,
    WorkstreamId,
    new_comment_id,
)
from lumberjack.ports.clock import Clock
from lumberjack.ports.ledger import Ledger

__all__ = ["HUMAN", "ReviewDesk"]

HUMAN = AgentId("human")
"""Comments are attributed to a person, not to the harness. The distinction matters
in a transcript where everything else was written by a model."""


@dataclass(slots=True)
class ReviewDesk:
    ledger: Ledger
    projections: Projections
    bus: LedgerMessageBus
    clock: Clock

    async def comment(
        self,
        *,
        body: str,
        file: RepoPath,
        line_start: int,
        line_end: int | None = None,
        side: str = "additions",
        workstream: WorkstreamId | None = None,
        conflict_id: ConflictId | None = None,
        author: AgentId = HUMAN,
    ) -> ReviewComment:
        comment = ReviewComment(
            comment_id=new_comment_id(),
            author=author,
            body=body,
            file=file,
            line_start=line_start,
            line_end=line_end or line_start,
            side=side,
            workstream=workstream,
            conflict_id=conflict_id,
            posted_at=self.clock.now(),
        )
        recipients = self._recipients(comment)
        await self.ledger.append(
            ReviewCommentPosted(comment=comment, notified=recipients), actor=author
        )
        for agent in recipients:
            await self.bus.send(
                frm=author,
                to=agent,
                subject=f"review comment on {comment.file}:{comment.lines}",
                body=(
                    f"{comment.body}\n\n"
                    "This is from a person, not a peer agent. Your work cannot land "
                    "until it is addressed or resolved."
                ),
            )
        return comment

    def _recipients(self, comment: ReviewComment) -> tuple[AgentId, ...]:
        if comment.conflict_id is not None:
            report = self.projections.conflicts.get(comment.conflict_id)
            if report is not None:
                return tuple(
                    agent
                    for agent in (self.projections.agent_of(item) for item in report.between)
                    if agent is not None
                )
        if comment.workstream is not None:
            agent = self.projections.agent_of(comment.workstream)
            return (agent,) if agent is not None else ()
        return ()

    async def resolve(self, comment_id: CommentId, *, by: AgentId = HUMAN) -> None:
        await self.ledger.append(ReviewCommentResolved(comment_id=comment_id, by=by))

    async def rule(
        self,
        *,
        conflict_id: ConflictId,
        resolution: Resolution,
        rationale: str,
        author: AgentId = HUMAN,
    ) -> Directive:
        """Record a human ruling in the same form the foreman issues.

        The core executes it through the same path, which is what makes it binding
        rather than advisory: leases are released and reassigned either way.
        """
        now: datetime = self.clock.now()
        directive = Directive(
            conflict_id=conflict_id,
            resolution=resolution,
            issued_by=author,
            rationale=rationale,
            issued_at=now,
        )
        await self.ledger.append(DirectiveIssued(directive=directive), actor=author)
        return directive

    def blocking(self, workstream: WorkstreamId) -> tuple[ReviewComment, ...]:
        """Unresolved comments that hold this workstream out of the merge train."""
        return self.projections.comments_for(workstream, unresolved_only=True)

    @staticmethod
    def escalation(reason: str) -> Resolution:
        return EscalateToForeman(reason=reason)
