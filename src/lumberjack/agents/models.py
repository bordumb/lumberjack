"""Choosing which model an agent runs against.

A provider that is overloaded should cost a task a few seconds, not the task.  Blocking
a workstream because Anthropic returned 529 wastes a worktree, a plan and everything the
agent had loaded into context, and the fix is a second model rather than a retry loop --
so the chain is configuration, not code.

PydanticAI already has exactly this in ``FallbackModel``, which tries each model in turn
on a provider-level failure.  This module is only the wiring: when no fallbacks are
configured the primary model name is passed through untouched, so nothing is resolved
eagerly and an agent can still be built with no credentials present.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic_ai.models import Model
from pydantic_ai.models.fallback import FallbackModel

__all__ = ["build_model"]


def build_model(
    primary: str | Model | None, fallbacks: Sequence[str | Model] = ()
) -> str | Model | None:
    """The primary model, or a :class:`FallbackModel` chain when fallbacks are configured.

    Returning the bare name in the common case matters: ``Agent(name,
    defer_model_check=True)`` resolves the provider lazily, and building a
    ``FallbackModel`` resolves every member eagerly instead.
    """
    if primary is None or not fallbacks:
        return primary
    return FallbackModel(primary, *fallbacks)
