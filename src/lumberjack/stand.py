"""The public entry point.

::

    async with Stand.open(config) as stand:
        outcome = await stand.run("add OTel tracing across the service layer")

``Stand.open`` is an async context manager: it creates the worktrees on entry and, on
exit, removes the clean ones while **preserving any worktree that still holds unlanded
work** -- and says so.  A crash never destroys an agent's output.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

from lumberjack.adapters.ast_indexer import AstIndexer
from lumberjack.adapters.clock import SystemClock
from lumberjack.adapters.git_cli import GitCli
from lumberjack.adapters.projecting import ProjectingLedger
from lumberjack.adapters.sqlite_ledger import SqliteLedger
from lumberjack.adapters.uv_gate import CommandGate
from lumberjack.core.projections import Projections
from lumberjack.core.services import Services
from lumberjack.core.supervisor import StandOutcome, Supervisor
from lumberjack.domain.task import TaskGraph
from lumberjack.domain.workstream import StandConfig
from lumberjack.ids import StandId, new_stand_id
from lumberjack.ports.clock import Clock
from lumberjack.ports.gate import Gate
from lumberjack.ports.git import GitBackend
from lumberjack.ports.indexer import SymbolIndexer
from lumberjack.ports.ledger import Ledger

__all__ = ["Stand"]


@dataclass(slots=True)
class Stand:
    """One swarm run: a goal, a task graph, N workstreams, one ledger."""

    stand_id: StandId
    config: StandConfig
    services: Services
    supervisor: Supervisor
    _owns_ledger: bool = True
    _preserved: list[str] = field(default_factory=list)

    # -- construction ----------------------------------------------------------------

    @classmethod
    async def create(
        cls,
        config: StandConfig,
        *,
        stand_id: StandId | None = None,
        git: GitBackend | None = None,
        ledger: Ledger | None = None,
        indexer: SymbolIndexer | None = None,
        gate: Gate | None = None,
        clock: Clock | None = None,
    ) -> Stand:
        stand = stand_id or new_stand_id()
        the_clock = clock or SystemClock()
        projections = Projections(stand=stand)
        owns_ledger = ledger is None
        inner = ledger or await SqliteLedger.open(
            stand, config.resolved_state_root() / stand / "ledger.db", clock=the_clock
        )
        services = Services.wire(
            stand=stand,
            config=config,
            clock=the_clock,
            git=git or GitCli(repo=config.repo),
            ledger=ProjectingLedger(inner=inner, projections=projections),
            indexer=indexer or AstIndexer(),
            gate=gate or CommandGate(commands=config.gate_commands),
            projections=projections,
        )
        return cls(
            stand_id=stand,
            config=config,
            services=services,
            supervisor=Supervisor(services=services),
            _owns_ledger=owns_ledger,
        )

    @classmethod
    @contextlib.asynccontextmanager
    async def open(
        cls,
        config: StandConfig,
        *,
        stand_id: StandId | None = None,
        git: GitBackend | None = None,
        ledger: Ledger | None = None,
        indexer: SymbolIndexer | None = None,
        gate: Gate | None = None,
        clock: Clock | None = None,
    ) -> AsyncIterator[Stand]:
        stand = await cls.create(
            config,
            stand_id=stand_id,
            git=git,
            ledger=ledger,
            indexer=indexer,
            gate=gate,
            clock=clock,
        )
        try:
            yield stand
        finally:
            await stand.close()

    # -- operation -------------------------------------------------------------------

    async def plan(self, goal: str) -> TaskGraph:
        """Scout the repository and decompose the goal, without running anything."""
        await self.supervisor.prepare(goal)
        return await self.supervisor.plan(goal)

    async def run(self, goal: str) -> StandOutcome:
        return await self.supervisor.run(goal)

    async def halt(self, reason: str = "operator halt") -> None:
        await self.supervisor.halt(reason)

    @property
    def state_dir(self) -> Path:
        return self.config.resolved_state_root() / self.stand_id

    # -- teardown --------------------------------------------------------------------

    async def close(self) -> None:
        """Remove clean worktrees; preserve anything holding unlanded work."""
        self.supervisor.stop.set()
        projections = self.services.projections
        for workstream in tuple(projections.workstreams.values()):
            landed = workstream.task in projections.landed
            try:
                if landed:
                    await self.services.git.remove_worktree(workstream.worktree, force=True)
                else:
                    self._preserved.append(str(workstream.worktree.path))
            except Exception:
                self._preserved.append(str(workstream.worktree.path))
        if self._owns_ledger:
            with contextlib.suppress(Exception):
                await self.services.ledger.close()

    @property
    def preserved(self) -> tuple[str, ...]:
        """Worktrees kept because they still hold work that never landed."""
        return tuple(self._preserved)
