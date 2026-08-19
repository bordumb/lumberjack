"""The in-process runner: a PydanticAI agent doing the work itself."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic_ai import Agent

from lumberjack.agents.deps import WorkerDeps
from lumberjack.agents.outputs import WorkerOutput
from lumberjack.core.services import Services
from lumberjack.domain.task import TaskSpec
from lumberjack.domain.workstream import Workstream

__all__ = ["PydanticAiRunner"]


@dataclass(slots=True)
class PydanticAiRunner:
    """Runs the built-in worker agent.  Metered against whatever provider it resolves."""

    agent: Agent[WorkerDeps, WorkerOutput]
    name: str = "pydantic_ai"

    async def preflight(self, services: Services) -> None:
        """Nothing to check: the model is resolved lazily and the agent runs in-process."""

    async def run(self, workstream: Workstream, spec: TaskSpec, services: Services) -> WorkerOutput:
        result = await self.agent.run(
            spec.intent,
            deps=WorkerDeps(
                services=services,
                identity=workstream.agent,
                workstream=workstream.workstream_id,
                task=spec,
                worktree=workstream.worktree,
            ),
        )
        # The only place a worker's ``RunUsage`` exists.  The supervisor holds the
        # ``WorkerOutput`` the port promises and nothing else, so accounting for the
        # tokens has to happen on this side of the boundary or not at all.
        services.usage.record(
            workstream.workstream_id,
            result.usage,
            agent=str(workstream.agent),
            model=services.config.model,
        )
        return result.output
