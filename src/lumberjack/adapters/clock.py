"""Clock adapters."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

__all__ = ["FrozenClock", "SystemClock"]


@dataclass(frozen=True, slots=True)
class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, delay: timedelta) -> None:
        await asyncio.sleep(max(0.0, delay.total_seconds()))


@dataclass(slots=True)
class FrozenClock:
    """Deterministic time for tests.  ``sleep`` advances the clock instead of waiting."""

    current: datetime = field(default_factory=lambda: datetime(2026, 1, 1, tzinfo=UTC))

    def now(self) -> datetime:
        return self.current

    async def sleep(self, delay: timedelta) -> None:
        """Advance instead of waiting -- but still yield, so background loops cannot
        starve the event loop when every sleep in the system is instantaneous."""
        self.current += delay
        await asyncio.sleep(0)

    def advance(self, delay: timedelta) -> datetime:
        self.current += delay
        return self.current
