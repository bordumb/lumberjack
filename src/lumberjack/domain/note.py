"""The blackboard: shared, append-only, topic-addressed notes."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from lumberjack.domain.claim import Scope
from lumberjack.domain.symbols import SymbolRef
from lumberjack.ids import AgentId, NoteId

__all__ = ["Note", "Topic"]


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
