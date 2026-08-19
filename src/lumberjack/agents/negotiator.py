"""The negotiating agent: one turn of a peer channel."""

from __future__ import annotations

from pydantic_ai import Agent
from pydantic_ai.models.instrumented import InstrumentationSettings

from lumberjack.agents.deps import NegotiatorDeps
from lumberjack.agents.instrumentation import QUIET
from lumberjack.agents.outputs import NegotiationOutput
from lumberjack.agents.prompts import NEGOTIATOR

__all__ = ["build_negotiator"]


def build_negotiator(
    model: str | None = None, *, instrument: InstrumentationSettings | bool = QUIET
) -> Agent[NegotiatorDeps, NegotiationOutput]:
    agent = Agent[NegotiatorDeps, NegotiationOutput](
        model,
        deps_type=NegotiatorDeps,
        output_type=NegotiationOutput,
        instructions=NEGOTIATOR,
        retries=2,
        defer_model_check=True,
        name="lumberjack-negotiator",
    )
    agent.instrument = instrument
    return agent
