"""How a workstream's task actually gets done.

The supervisor does not care whether a task is executed by a PydanticAI agent in this
process or by some other agent in a subprocess -- it cares that something returns a
typed :data:`WorkerOutput`.  That indifference is what lets a Claude Code session,
billed to a flat-rate plan rather than metered API tokens, do the work instead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from lumberjack.agents.outputs import WorkerOutput
    from lumberjack.core.services import Services
    from lumberjack.domain.task import TaskSpec
    from lumberjack.domain.workstream import Workstream

__all__ = ["WorkerRunner"]


class WorkerRunner(Protocol):
    @property
    def name(self) -> str: ...

    async def preflight(self, services: Services) -> None:
        """Verify everything this runner needs, before any work is spawned.

        Raise to abort the stand.  A swarm whose coordination is broken still burns
        tokens and still writes code -- it writes it blind, while the harness reports
        healthy progress -- so failing here is strictly better than starting.
        """
        ...

    async def run(
        self, workstream: Workstream, spec: TaskSpec, services: Services
    ) -> WorkerOutput: ...
