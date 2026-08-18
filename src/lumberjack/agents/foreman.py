"""The foreman: decomposition, contracts, and binding rulings on escalated conflicts.

It is deliberately not in the path of ordinary work.  If the foreman is busy, or
absent, the workers keep working.
"""

from __future__ import annotations

from pydantic_ai import Agent, RunContext

from lumberjack.agents.deps import ForemanDeps
from lumberjack.agents.outputs import ForemanRuling, Plan
from lumberjack.agents.prompts import FOREMAN

__all__ = ["build_arbiter", "build_planner"]


def build_planner(model: str | None = None) -> Agent[ForemanDeps, Plan]:
    agent = Agent[ForemanDeps, Plan](
        model,
        deps_type=ForemanDeps,
        output_type=Plan,
        instructions=FOREMAN,
        retries=3,
        defer_model_check=True,
        name="lumberjack-foreman-planner",
    )

    @agent.instructions
    def repo_brief(ctx: RunContext[ForemanDeps]) -> str:
        repo_map = ctx.deps.repo_map
        modules = "\n".join(
            f"  {node.module} ({node.path}) imports {len(node.imports)}"
            for node in repo_map.modules[:60]
        )
        hot = ", ".join(repo_map.hot_paths(8)) or "(unknown)"
        return (
            f"GOAL: {ctx.deps.goal}\n\n"
            f"Repository map ({len(repo_map.modules)} modules):\n{modules or '  (empty repo)'}\n\n"
            f"Most-changed files: {hot}\n"
            f"Tests: {', '.join(repo_map.test_paths[:10]) or '(none found)'}\n\n"
            f"Plan for at most {ctx.deps.services.config.max_parallel} parallel workstreams."
        )

    return agent


def build_arbiter(model: str | None = None) -> Agent[ForemanDeps, ForemanRuling]:
    return Agent[ForemanDeps, ForemanRuling](
        model,
        deps_type=ForemanDeps,
        output_type=ForemanRuling,
        instructions=FOREMAN,
        retries=2,
        defer_model_check=True,
        name="lumberjack-foreman-arbiter",
    )
