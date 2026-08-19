"""The worker agent."""

from __future__ import annotations

from pydantic_ai import Agent, RunContext
from pydantic_ai.models import Model
from pydantic_ai.models.instrumented import InstrumentationSettings

from lumberjack.agents.deps import WorkerDeps
from lumberjack.agents.instrumentation import QUIET
from lumberjack.agents.outputs import (
    TaskBlocked,
    TaskCompleted,
    TaskNeedsSplit,
    WorkerOutput,
)
from lumberjack.agents.prompts import WORKER
from lumberjack.agents.toolsets import coordination_toolset
from lumberjack.agents.workspace import workspace_toolset

__all__ = ["build_worker", "worker"]


def build_worker(
    model: str | Model | None = None, *, instrument: InstrumentationSettings | bool = QUIET
) -> Agent[WorkerDeps, WorkerOutput]:
    agent = Agent[WorkerDeps, WorkerOutput](
        model,
        deps_type=WorkerDeps,
        output_type=[TaskCompleted, TaskBlocked, TaskNeedsSplit],
        instructions=WORKER,
        toolsets=[coordination_toolset, workspace_toolset],
        retries=2,
        defer_model_check=True,
        name="lumberjack-worker",
    )
    # Set after construction: `instrument` is a property on Agent in pydantic-ai 1.x,
    # not a constructor argument.
    agent.instrument = instrument

    @agent.instructions
    def task_brief(ctx: RunContext[WorkerDeps]) -> str:
        spec = ctx.deps.task
        acceptance = "\n".join(f"  - {item}" for item in spec.acceptance) or "  - (none given)"
        return (
            f"You are {ctx.deps.identity} in workstream {ctx.deps.workstream}.\n"
            f"Your worktree: {ctx.deps.worktree.path} (branch {ctx.deps.worktree.branch}).\n\n"
            f"TASK {spec.task_id}: {spec.title}\n{spec.intent}\n\n"
            f"Acceptance criteria:\n{acceptance}"
        )

    @agent.instructions
    def awareness(ctx: RunContext[WorkerDeps]) -> str:
        """Recomputed on every step, so it cannot go stale mid-run."""
        return ctx.deps.services.digest.render(ctx.deps.workstream)

    return agent


worker: Agent[WorkerDeps, WorkerOutput] = build_worker()
"""Model-less default; bind one per run with ``agent.override`` or ``build_worker``."""
