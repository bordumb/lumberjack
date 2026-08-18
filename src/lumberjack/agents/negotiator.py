"""The negotiating agent: one turn of a peer channel."""

from __future__ import annotations

from pydantic_ai import Agent

from lumberjack.agents.deps import NegotiatorDeps
from lumberjack.agents.outputs import NegotiationOutput
from lumberjack.agents.prompts import NEGOTIATOR

__all__ = ["build_negotiator"]


def build_negotiator(model: str | None = None) -> Agent[NegotiatorDeps, NegotiationOutput]:
    return Agent[NegotiatorDeps, NegotiationOutput](
        model,
        deps_type=NegotiatorDeps,
        output_type=NegotiationOutput,
        instructions=NEGOTIATOR,
        retries=2,
        defer_model_check=True,
        name="lumberjack-negotiator",
    )
