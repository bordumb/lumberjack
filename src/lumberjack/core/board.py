"""The blackboard and the mailboxes.

Both are ledger projections rather than side channels, which is what makes "why did
agent B rewrite that module?" answerable by replay, negotiation transcript included.
"""

from __future__ import annotations

from dataclasses import dataclass

from lumberjack.core.projections import Projections
from lumberjack.domain.claim import Scope, scopes_overlap
from lumberjack.domain.events import MessageRead, MessageSent, NotePosted
from lumberjack.domain.message import Message, Recipient
from lumberjack.domain.note import Note
from lumberjack.domain.symbols import SymbolRef
from lumberjack.ids import AgentId, MessageId, NoteId, new_message_id, new_note_id
from lumberjack.ports.clock import Clock
from lumberjack.ports.ledger import Ledger

__all__ = ["LedgerBlackboard", "LedgerMessageBus"]


@dataclass(slots=True)
class LedgerBlackboard:
    ledger: Ledger
    projections: Projections
    clock: Clock

    async def post(
        self,
        *,
        author: AgentId,
        topic: str,
        body: str,
        scope: Scope | None = None,
        pins: tuple[SymbolRef, ...] = (),
    ) -> NoteId:
        note = Note(
            note_id=new_note_id(),
            author=author,
            topic=topic,
            body=body,
            scope=scope,
            pins=pins,
            posted_at=self.clock.now(),
        )
        await self.ledger.append(NotePosted(note=note), actor=author)
        return note.note_id

    async def read(
        self, *, topic: str | None = None, scope: Scope | None = None, limit: int = 20
    ) -> tuple[Note, ...]:
        found = [
            note
            for note in self.projections.notes
            if (topic is None or note.topic == topic)
            and (scope is None or note.scope is None or scopes_overlap(note.scope, scope))
        ]
        return tuple(reversed(found[-limit:]))


@dataclass(slots=True)
class LedgerMessageBus:
    ledger: Ledger
    projections: Projections
    clock: Clock

    async def send(
        self,
        *,
        frm: AgentId,
        to: Recipient,
        subject: str,
        body: str,
        in_reply_to: MessageId | None = None,
        conflict_id: str | None = None,
    ) -> MessageId:
        message = Message(
            message_id=new_message_id(),
            frm=frm,
            to=to,
            subject=subject,
            body=body,
            sent_at=self.clock.now(),
            in_reply_to=in_reply_to,
            conflict_id=conflict_id,  # type: ignore[arg-type]
        )
        await self.ledger.append(MessageSent(message=message), actor=frm)
        return message.message_id

    async def inbox(self, agent: AgentId, *, unread_only: bool = True) -> tuple[Message, ...]:
        if unread_only:
            return self.projections.unread_for(agent)
        return tuple(
            message
            for _, message in self.projections.messages
            if message.addressed_to(agent) and message.frm != agent
        )

    async def mark_read(self, agent: AgentId) -> None:
        await self.ledger.append(
            MessageRead(reader=agent, up_to=await self.ledger.latest()), actor=agent
        )
