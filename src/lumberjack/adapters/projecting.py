"""A ledger wrapper that folds every append into a projection as it is written.

Without this, projections lag the log by however long a subscription takes to fire,
and a tool that appends an event and immediately queries the derived state sees the
world as it was a moment ago.  Folding on write makes read-your-own-writes total.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

from lumberjack.core.projections import Projections
from lumberjack.domain.events import Actor, Envelope, EventPayload
from lumberjack.ids import Seq
from lumberjack.ports.ledger import Ledger

__all__ = ["ProjectingLedger"]


@dataclass(slots=True)
class ProjectingLedger:
    inner: Ledger
    projections: Projections

    async def append(self, payload: EventPayload, *, actor: Actor = "system") -> Seq:
        seq = await self.inner.append(payload, actor=actor)
        for envelope in await self.inner.read(since=Seq(seq - 1), limit=1):
            self.projections.apply(envelope)
        return seq

    async def read(
        self,
        *,
        since: Seq | None = None,
        kinds: frozenset[str] | None = None,
        limit: int | None = None,
    ) -> tuple[Envelope[EventPayload], ...]:
        return await self.inner.read(since=since, kinds=kinds, limit=limit)

    def subscribe(
        self, *, kinds: frozenset[str] | None = None, since: Seq | None = None
    ) -> AsyncIterator[Envelope[EventPayload]]:
        return self.inner.subscribe(kinds=kinds, since=since)

    async def latest(self) -> Seq:
        return await self.inner.latest()

    async def close(self) -> None:
        await self.inner.close()
