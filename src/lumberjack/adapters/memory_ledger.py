"""In-memory ledger.  The reference implementation and the one tests use."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from lumberjack.adapters.clock import SystemClock
from lumberjack.domain.events import Actor, Envelope, EventPayload
from lumberjack.ids import Seq, StandId
from lumberjack.ports.clock import Clock

__all__ = ["MemoryLedger"]


@dataclass(slots=True)
class MemoryLedger:
    stand: StandId
    clock: Clock = field(default_factory=SystemClock)
    _events: list[Envelope[EventPayload]] = field(default_factory=list)
    _waiters: list[asyncio.Event] = field(default_factory=list)
    _closed: bool = False

    async def append(self, payload: EventPayload, *, actor: Actor = "system") -> Seq:
        seq = Seq(len(self._events) + 1)
        self._events.append(
            Envelope(seq=seq, at=self.clock.now(), stand=self.stand, actor=actor, payload=payload)
        )
        for waiter in self._waiters:
            waiter.set()
        return seq

    async def read(
        self,
        *,
        since: Seq | None = None,
        kinds: frozenset[str] | None = None,
        limit: int | None = None,
    ) -> tuple[Envelope[EventPayload], ...]:
        found = [
            envelope
            for envelope in self._events
            if (since is None or envelope.seq > since) and (kinds is None or envelope.kind in kinds)
        ]
        return tuple(found[-limit:] if limit is not None else found)

    async def latest(self) -> Seq:
        return Seq(len(self._events))

    async def subscribe(
        self, *, kinds: frozenset[str] | None = None, since: Seq | None = None
    ) -> AsyncIterator[Envelope[EventPayload]]:
        cursor = since if since is not None else Seq(0)
        signal = asyncio.Event()
        self._waiters.append(signal)
        try:
            while not self._closed:
                batch = await self.read(since=cursor, kinds=kinds)
                if batch:
                    for envelope in batch:
                        cursor = envelope.seq
                        yield envelope
                    continue
                signal.clear()
                await signal.wait()
        finally:
            self._waiters.remove(signal)

    async def close(self) -> None:
        self._closed = True
        for waiter in self._waiters:
            waiter.set()
