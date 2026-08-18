"""The quality bar."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from lumberjack.domain.gate import GateReport
    from lumberjack.domain.workstream import Worktree

__all__ = ["Gate"]


class Gate(Protocol):
    async def run(self, worktree: Worktree) -> GateReport: ...
