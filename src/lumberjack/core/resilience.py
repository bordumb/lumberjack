"""Failing loudly, and stopping.

A background loop that swallows every exception is worse than one that crashes: the
stand keeps running, the thing the loop was for silently stops happening, and nothing
anywhere says so.  Everything here exists to make that impossible.

:class:`LoopGuard` runs one iteration of a loop, and on failure does three things --
records a :class:`~lumberjack.domain.events.ComponentFailed` event, counts consecutive
failures, and **stops the loop** once they pass a threshold.  Spinning on a
deterministic failure for the length of a stand buys nothing and hides everything.

:class:`ArtifactStore` keeps the tracebacks.  An event carries a reason; a traceback is
too big for the ledger and too valuable to drop, so it goes to a file the event points
at.
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from lumberjack.domain.errors import is_transient
from lumberjack.domain.events import ComponentFailed
from lumberjack.ids import ArtifactRef, WorkstreamId
from lumberjack.ports.ledger import Ledger

__all__ = ["ArtifactStore", "LoopGuard"]

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ArtifactStore:
    """Somewhere durable to put the detail that does not belong in an event."""

    root: Path

    def write(self, name: str, body: str) -> ArtifactRef | None:
        """Write ``body`` and return a reference, or ``None`` if it could not be written.

        Losing an artifact must never turn into a second failure on top of the first
        one, so this reports rather than raises -- and it says so in the log either way.
        """
        target = self.root / f"{name}-{uuid4().hex[:8]}.txt"
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        except OSError as error:
            log.warning("could not write artifact %s: %s", target, error)
            return None
        return ArtifactRef(str(target))

    def read(self, ref: ArtifactRef) -> str | None:
        path = Path(ref)
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None


@dataclass(slots=True)
class LoopGuard:
    """One background loop's tolerance for failure.

    ``attempt`` returns ``False`` when the loop should stop -- either because it has
    failed ``limit`` times running, or because it was asked to shut down.
    """

    component: str
    ledger: Ledger
    limit: int = 3
    artifacts: ArtifactStore | None = None
    workstream: WorkstreamId | None = None
    consecutive: int = field(default=0, init=False)
    stopped: bool = field(default=False, init=False)

    async def attempt(self, body: Callable[[], Awaitable[None]]) -> bool:
        """Run one iteration.  ``False`` means the caller must leave the loop."""
        try:
            await body()
        except asyncio.CancelledError:
            # Cancellation is the shutdown path, not a component failure. Re-raising
            # keeps `_shutdown`'s bounded wait honest.
            raise
        except Exception as error:
            # Deliberately broad, and the one place in the codebase where that is
            # right: a loop body reaches arbitrary adapters, and the whole point of
            # this class is that *every* way it can fail is recorded and counted
            # rather than swallowed. Nothing is silent here.
            return await self.failed(error)
        self.consecutive = 0
        return True

    async def failed(self, error: BaseException) -> bool:
        """Record a failure.  ``False`` means the threshold has been reached."""
        self.consecutive += 1
        giving_up = self.consecutive >= self.limit
        self.stopped = giving_up
        ref = self._capture(error)
        log.error(
            "%s failed (%d consecutive, %s, %s): %s",
            self.component,
            self.consecutive,
            "transient" if is_transient(error) else "terminal",
            "giving up" if giving_up else "continuing",
            error,
            exc_info=error,
        )
        await self.ledger.append(
            ComponentFailed(
                component=self.component,
                error=f"{type(error).__name__}: {error}"[:500],
                consecutive=self.consecutive,
                giving_up=giving_up,
                traceback_ref=ref,
                workstream=self.workstream,
            )
        )
        return not giving_up

    def _capture(self, error: BaseException) -> ArtifactRef | None:
        if self.artifacts is None:
            return None
        body = "".join(traceback.format_exception(error))
        return self.artifacts.write(self.component, body)
