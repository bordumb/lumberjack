"""Durable ledger on SQLite in WAL mode.

One writer (the coordinator), many readers -- including agent processes and ``lj watch``
-- which is why the discriminator lives in its own column: subscriptions filter in SQL
rather than deserializing every row.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import aiosqlite
from pydantic import TypeAdapter

from lumberjack.adapters.clock import SystemClock
from lumberjack.domain.events import Actor, Envelope, EventPayload
from lumberjack.ids import Seq, StandId
from lumberjack.ports.clock import Clock

__all__ = ["SqliteLedger"]

_PAYLOAD: TypeAdapter[EventPayload] = TypeAdapter(EventPayload)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq     INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT    NOT NULL,
    stand   TEXT    NOT NULL,
    actor   TEXT    NOT NULL,
    kind    TEXT    NOT NULL,
    payload TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS events_kind ON events(kind);
CREATE INDEX IF NOT EXISTS events_stand ON events(stand);
"""


@dataclass(slots=True)
class SqliteLedger:
    stand: StandId
    path: Path
    clock: Clock = field(default_factory=SystemClock)
    poll_interval: float = 0.25
    _db: aiosqlite.Connection | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _closed: bool = False

    @classmethod
    async def open(cls, stand: StandId, path: Path, *, clock: Clock | None = None) -> SqliteLedger:
        path.parent.mkdir(parents=True, exist_ok=True)
        ledger = cls(stand=stand, path=path, clock=clock or SystemClock())
        db = await aiosqlite.connect(str(path))
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.executescript(_SCHEMA)
        await db.commit()
        ledger._db = db
        return ledger

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            msg = "ledger is not open; use SqliteLedger.open()"
            raise RuntimeError(msg)
        return self._db

    async def append(self, payload: EventPayload, *, actor: Actor = "system") -> Seq:
        async with self._lock:
            cursor = await self._conn.execute(
                "INSERT INTO events (at, stand, actor, kind, payload) VALUES (?,?,?,?,?)",
                (
                    self.clock.now().isoformat(),
                    self.stand,
                    actor,
                    payload.kind,
                    payload.model_dump_json(),
                ),
            )
            await self._conn.commit()
            return Seq(int(cursor.lastrowid or 0))

    async def read(
        self,
        *,
        since: Seq | None = None,
        kinds: frozenset[str] | None = None,
        limit: int | None = None,
    ) -> tuple[Envelope[EventPayload], ...]:
        clauses = ["stand = ?"]
        params: list[object] = [self.stand]
        if since is not None:
            clauses.append("seq > ?")
            params.append(int(since))
        if kinds is not None:
            if not kinds:
                return ()
            clauses.append(f"kind IN ({','.join('?' * len(kinds))})")
            params.extend(sorted(kinds))
        query = f"SELECT seq, at, stand, actor, kind, payload FROM events WHERE {' AND '.join(clauses)} ORDER BY seq"  # noqa: E501
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))
        async with self._conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()
        return tuple(_row_to_envelope(row) for row in rows)

    async def latest(self) -> Seq:
        async with self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM events WHERE stand = ?", (self.stand,)
        ) as cursor:
            row = await cursor.fetchone()
        return Seq(int(row[0]) if row else 0)

    async def subscribe(
        self, *, kinds: frozenset[str] | None = None, since: Seq | None = None
    ) -> AsyncIterator[Envelope[EventPayload]]:
        """Polling tail.  Cross-process by construction, which in-process signalling is not."""
        cursor = since if since is not None else Seq(0)
        while not self._closed:
            batch = await self.read(since=cursor, kinds=kinds)
            if batch:
                for envelope in batch:
                    cursor = envelope.seq
                    yield envelope
                continue
            await asyncio.sleep(self.poll_interval)

    async def close(self) -> None:
        self._closed = True
        if self._db is not None:
            await self._db.close()
            self._db = None


def _row_to_envelope(row: Sequence[object]) -> Envelope[EventPayload]:
    seq, at, stand, actor, _kind, payload = row
    return Envelope(
        seq=Seq(int(str(seq))),
        at=str(at),  # pydantic parses the ISO string
        stand=StandId(str(stand)),
        actor=str(actor),
        payload=_PAYLOAD.validate_json(str(payload)),
    )
