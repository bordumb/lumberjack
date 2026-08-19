"""The blackboard: shared, append-only, topic-addressed notes."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from lumberjack.domain.claim import Scope
from lumberjack.domain.symbols import SymbolRef
from lumberjack.ids import (
    AgentId,
    CommentId,
    ConflictId,
    NoteId,
    RepoPath,
    WorkstreamId,
)

__all__ = ["Note", "ReviewComment", "Topic"]


class Topic:
    """Conventional topics.  Free-form strings are allowed; these are just the defaults."""

    DECISIONS = "decisions"
    CONVENTIONS = "conventions"
    GOTCHAS = "gotchas"
    HANDOFF = "handoff"


class Note(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    note_id: NoteId
    author: AgentId
    topic: str = Field(min_length=1, max_length=64)
    body: str = Field(min_length=1, max_length=2000)
    scope: Scope | None = None
    pins: tuple[SymbolRef, ...] = ()
    posted_at: datetime

    def render(self) -> str:
        where = f" ({self.scope.describe()})" if self.scope is not None else ""
        return f"[{self.topic}] {self.author}{where}: {self.body}"


class ReviewComment(BaseModel):
    """A human's comment on a line, addressed into the swarm.

    The point is not to record an opinion somewhere a person can find it later. It is
    to put the comment where the agent working on that file will read it before it
    writes the next line -- which is why this carries a scope and lands in the digest.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    comment_id: CommentId
    author: AgentId
    body: str = Field(min_length=1, max_length=4000)
    file: RepoPath
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    side: str = "additions"
    workstream: WorkstreamId | None = None
    conflict_id: ConflictId | None = None
    posted_at: datetime
    resolved: bool = False

    @property
    def lines(self) -> str:
        return (
            str(self.line_start)
            if self.line_start == self.line_end
            else f"{self.line_start}-{self.line_end}"
        )

    def render(self) -> str:
        return f"{self.file}:{self.lines} — {self.body}"
