"""The append-only event log."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from lumberjack.domain.events import Actor, Envelope, EventPayload
    from lumberjack.ids import Seq

__all__ = ["Ledger"]


class Ledger(Protocol):
    async def append(self, payload: EventPayload, *, actor: Actor = "system") -> Seq: ...

    async def read(
        self,
        *,
        since: Seq | None = None,
        kinds: frozenset[str] | None = None,
        limit: int | None = None,
    ) -> tuple[Envelope[EventPayload], ...]: ...

    def subscribe(
        self, *, kinds: frozenset[str] | None = None, since: Seq | None = None
    ) -> AsyncIterator[Envelope[EventPayload]]: ...

    async def latest(self) -> Seq: ...

    async def close(self) -> None: ...
