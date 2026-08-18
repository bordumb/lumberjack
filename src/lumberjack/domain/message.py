"""Direct agent-to-agent messages.

Delivery is *pull, not interrupt*: unread messages surface in the awareness digest at
the next turn boundary, so a chatty peer cannot derail a worker mid-edit.  The one
exception is handled by the supervisor, not here: a ``BLOCK`` conflict preempts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from lumberjack.ids import AgentId, ConflictId, MessageId

__all__ = ["BROADCAST", "Message", "Recipient"]

BROADCAST: Literal["broadcast"] = "broadcast"

Recipient = AgentId | Literal["broadcast"]


class Message(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    message_id: MessageId
    frm: AgentId
    to: Recipient
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=4000)
    sent_at: datetime
    in_reply_to: MessageId | None = None
    conflict_id: ConflictId | None = None

    def addressed_to(self, agent: AgentId) -> bool:
        return self.to == BROADCAST or self.to == agent

    def render(self) -> str:
        return f"from {self.frm} -- {self.subject}: {self.body}"
