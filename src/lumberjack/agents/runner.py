"""The in-process runner: a PydanticAI agent doing the work itself."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic_ai import Agent
from pydantic_ai.usage import UsageLimits

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
            # `Budget.max_steps_per_task`, enforced where the steps actually happen.
            # A runaway tool loop is stopped by the agent run itself rather than by an
            # operator noticing the bill, and the supervisor turns the resulting
            # `UsageLimitExceeded` into BlockReason.BUDGET_EXHAUSTED.
            usage_limits=UsageLimits(request_limit=services.config.budget.max_steps_per_task),
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
