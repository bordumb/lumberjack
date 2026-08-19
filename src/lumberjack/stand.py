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
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

from lumberjack.adapters.ast_indexer import AstIndexer
from lumberjack.adapters.clock import SystemClock
from lumberjack.adapters.git_cli import GitCli
from lumberjack.adapters.otel import build_telemetry
from lumberjack.adapters.projecting import ProjectingLedger
from lumberjack.adapters.retrying import RetryingGit
from lumberjack.adapters.sqlite_ledger import SqliteLedger
from lumberjack.adapters.traced import instrumented
from lumberjack.adapters.uv_gate import CommandGate
from lumberjack.core.projections import Projections
from lumberjack.core.services import Services
from lumberjack.core.supervisor import StandOutcome, Supervisor
from lumberjack.domain.task import TaskGraph
from lumberjack.domain.workstream import PreservedWorktree, StandConfig
from lumberjack.ids import StandId, new_stand_id
from lumberjack.ports.clock import Clock
from lumberjack.ports.gate import Gate
from lumberjack.ports.git import GitBackend, GitError
from lumberjack.ports.indexer import SymbolIndexer
from lumberjack.ports.ledger import Ledger

__all__ = ["Stand"]

log = logging.getLogger(__name__)


def _resilient(git: GitBackend, clock: Clock) -> GitBackend:
    """Wrap a git backend so lock contention between sibling worktrees is retried.

    Wrapping once, here, is the whole change: no call site anywhere below knows that
    retrying happens, and a backend a caller supplied for a test is wrapped on exactly
    the same terms as the real one.
    """
    return git if isinstance(git, RetryingGit) else RetryingGit(inner=git, clock=clock)


@dataclass(slots=True)
class Stand:
    """One swarm run: a goal, a task graph, N workstreams, one ledger."""

    stand_id: StandId
    config: StandConfig
    services: Services
    supervisor: Supervisor
    _owns_ledger: bool = True
    _preserved: list[PreservedWorktree] = field(default_factory=list)

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
        telemetry = build_telemetry(config.telemetry)
        # Tracing outside retrying, so a span reports what the *caller* waited for --
        # a merge_tree that took three attempts cost the oracle all three.
        traced_git, traced_gate = instrumented(
            git=_resilient(git or GitCli(repo=config.repo), the_clock),
            gate=gate or CommandGate(commands=config.gate_commands),
            telemetry=telemetry,
        )
        services = Services.wire(
            stand=stand,
            config=config,
            clock=the_clock,
            git=traced_git,
            ledger=ProjectingLedger(inner=inner, projections=projections),
            indexer=indexer or AstIndexer(),
            gate=traced_gate,
            projections=projections,
            telemetry=telemetry,
        )
        return cls(
            stand_id=stand,
            config=config,
            services=services,
            supervisor=Supervisor(services=services),
            _owns_ledger=owns_ledger,
        )

    @classmethod
    async def attach(
        cls,
        stand_id: StandId,
        config: StandConfig | None = None,
        *,
        git: GitBackend | None = None,
        clock: Clock | None = None,
    ) -> Stand:
        """Open an existing stand for another session.

        The configuration comes from the stand's own ``StandStarted`` event rather than
        from the caller: a run continues the way it began, and rediscovering its runtime
        from a config file is how a resumed run ends up on a different one.
        """
        the_clock = clock or SystemClock()
        projections = Projections(stand=stand_id)
        provisional = config or StandConfig()
        inner = await SqliteLedger.open(
            stand_id,
            provisional.resolved_state_root() / stand_id / "ledger.db",
            clock=the_clock,
        )
        ledger = ProjectingLedger(inner=inner, projections=projections)
        await projections.hydrate(ledger)

        recorded = projections.config or provisional
        if config is not None:
            recorded = recorded.model_copy(update={"repo": config.repo})

        telemetry = build_telemetry(recorded.telemetry)
        traced_git, traced_gate = instrumented(
            git=_resilient(git or GitCli(repo=recorded.repo), the_clock),
            gate=CommandGate(commands=recorded.gate_commands),
            telemetry=telemetry,
        )
        services = Services.wire(
            stand=stand_id,
            config=recorded,
            clock=the_clock,
            git=traced_git,
            ledger=ledger,
            indexer=AstIndexer(),
            gate=traced_gate,
            projections=projections,
            telemetry=telemetry,
        )
        return cls(
            stand_id=stand_id,
            config=recorded,
            services=services,
            supervisor=Supervisor(services=services),
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

    async def resume(self) -> StandOutcome:
        """Continue this stand in a new session."""
        return await self.supervisor.resume()

    async def halt(self, reason: str = "operator halt") -> None:
        await self.supervisor.halt(reason)

    @property
    def state_dir(self) -> Path:
        return self.config.resolved_state_root() / self.stand_id

    # -- teardown --------------------------------------------------------------------

    async def close(self) -> None:
        """Remove clean worktrees; preserve anything holding unlanded work.

        Two very different things used to arrive here as one list. A worktree kept
        because its work never landed is the design working as intended. A worktree kept
        because ``git worktree remove`` *failed* is a directory the operator now owns,
        and reporting it as the first is how it stayed invisible.
        """
        self.supervisor.stop.set()
        projections = self.services.projections
        halted = projections.halted
        for workstream in tuple(projections.workstreams.values()):
            path = str(workstream.worktree.path)
            if halted:
                # A halted run is interrupted, not finished. Even work that landed is
                # worth leaving on disk for whoever comes to look at why it stopped.
                log.info("preserving %s: the stand halted", path)
                self._preserved.append(PreservedWorktree(path=path, reason="halted"))
                continue
            if workstream.task not in projections.landed:
                log.info("preserving %s: task %s never landed", path, workstream.task)
                self._preserved.append(PreservedWorktree(path=path, reason="unlanded"))
                continue
            try:
                await self.services.git.remove_worktree(workstream.worktree, force=True)
            except (GitError, OSError) as error:
                # Never destroy work to tidy up: a removal that failed leaves the
                # worktree exactly where it was, and the operator is told so.
                log.warning("could not remove worktree %s: %s", path, error)
                self._preserved.append(
                    PreservedWorktree(path=path, reason="cleanup_failed", detail=str(error)[:300])
                )
        if self._owns_ledger:
            try:
                await self.services.ledger.close()
            except (OSError, RuntimeError) as error:
                # The run is over and the events are already durable; a failed close
                # loses nothing but must not be the last thing that happens silently.
                log.warning("ledger did not close cleanly: %s", error)

    @property
    def preserved(self) -> tuple[str, ...]:
        """Paths of every worktree that survived teardown, for whatever reason."""
        return tuple(item.path for item in self._preserved)

    @property
    def preserved_worktrees(self) -> tuple[PreservedWorktree, ...]:
        """The same worktrees, each with the reason it is still there."""
        return tuple(self._preserved)

    @property
    def cleanup_failures(self) -> tuple[PreservedWorktree, ...]:
        """Worktrees still on disk because removing them failed.  Operator's problem."""
        return tuple(item for item in self._preserved if item.reason == "cleanup_failed")
