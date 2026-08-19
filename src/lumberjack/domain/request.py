"""A request to start a stand.

The web UI and the CLI both need to describe "run these agents on this repository",
and the description has to survive crossing a process boundary. Rather than grow a
second, looser vocabulary of flags, a request is a document with the same validation
as everything else here -- so an unbuildable run fails where it is written, not three
subprocesses later.

Which model runs a task is deliberately *not* part of :class:`TaskSpec`. A task is the
work; the model is who was asked to do it, and the same task can be reassigned.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lumberjack.domain.task import TaskGraph, TaskSpec
from lumberjack.ids import TaskId

__all__ = [
    "MODELS",
    "AgentAssignment",
    "ModelChoice",
    "Provider",
    "RunRequest",
    "known_models",
]


class Provider(StrEnum):
    ANTHROPIC = "anthropic"


class ModelChoice(BaseModel):
    """One selectable model, as the picker should present it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: Provider
    id: str
    label: str
    note: str = ""
    default: bool = False

    @property
    def qualified(self) -> str:
        """The form PydanticAI expects: ``provider:model``."""
        return f"{self.provider.value}:{self.id}"


MODELS: tuple[ModelChoice, ...] = (
    ModelChoice(
        provider=Provider.ANTHROPIC,
        id="claude-opus-5",
        label="Opus 5",
        note="strongest; the default for work that has to land",
        default=True,
    ),
    ModelChoice(
        provider=Provider.ANTHROPIC,
        id="claude-sonnet-5",
        label="Sonnet 5",
        note="faster and cheaper; good for well-specified tasks",
    ),
    ModelChoice(
        provider=Provider.ANTHROPIC,
        id="claude-haiku-4-5-20251001",
        label="Haiku 4.5",
        note="fastest; mechanical changes only",
    ),
)


def known_models(provider: Provider | None = None) -> tuple[ModelChoice, ...]:
    return tuple(item for item in MODELS if provider is None or item.provider is provider)


class AgentAssignment(BaseModel):
    """One agent: a task, and who was asked to do it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task: TaskSpec
    provider: Provider = Provider.ANTHROPIC
    model: str = "claude-opus-5"

    @property
    def qualified_model(self) -> str:
        return f"{self.provider.value}:{self.model}"


class RunRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    runtime: Literal["claude_code", "pydantic_ai"] = "claude_code"
    agents: tuple[AgentAssignment, ...] = Field(min_length=1, max_length=16)
    arbitration: str | None = None

    @model_validator(mode="after")
    def _distinct_tasks(self) -> Self:
        ids = [assignment.task.task_id for assignment in self.agents]
        if len(set(ids)) != len(ids):
            msg = "two agents were given the same task id"
            raise ValueError(msg)
        return self

    def task_specs(self) -> tuple[TaskSpec, ...]:
        """The tasks this request describes.

        The domain does not reach up into the agent layer to build a ``Plan``; the CLI
        does that, which keeps the dependency pointing one way and the return type
        honest rather than ``object``.
        """
        return tuple(assignment.task for assignment in self.agents)

    def graph(self) -> TaskGraph:
        """The same tasks as a validated DAG, which is what makes the request runnable."""
        return TaskGraph(tasks=self.task_specs())

    def models(self) -> dict[TaskId, str]:
        """Per-task model overrides, keyed the way the supervisor assigns work."""
        return {assignment.task.task_id: assignment.qualified_model for assignment in self.agents}
