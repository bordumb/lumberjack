"""Mailboxes and the blackboard.

Both are ledger projections, so direct messages and shared notes are as auditable and
replayable as everything else in the system.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from lumberjack.domain.claim import Scope
    from lumberjack.domain.message import Message, Recipient
    from lumberjack.domain.note import Note
    from lumberjack.domain.symbols import SymbolRef
    from lumberjack.ids import AgentId, MessageId, NoteId

__all__ = ["Blackboard", "MessageBus"]


class MessageBus(Protocol):
    async def send(
        self,
        *,
        frm: AgentId,
        to: Recipient,
        subject: str,
        body: str,
        in_reply_to: MessageId | None = None,
    ) -> MessageId: ...

    async def inbox(self, agent: AgentId, *, unread_only: bool = True) -> tuple[Message, ...]: ...

    async def mark_read(self, agent: AgentId) -> None: ...


class Blackboard(Protocol):
    async def post(
        self,
        *,
        author: AgentId,
        topic: str,
        body: str,
        scope: Scope | None = None,
        pins: tuple[SymbolRef, ...] = (),
    ) -> NoteId: ...

    async def read(
        self, *, topic: str | None = None, scope: Scope | None = None, limit: int = 20
    ) -> tuple[Note, ...]: ...
