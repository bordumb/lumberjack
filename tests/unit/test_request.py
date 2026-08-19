"""The run request: what the UI hands the CLI, validated where it is written."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from lumberjack.domain.request import MODELS, AgentAssignment, Provider, RunRequest, known_models
from lumberjack.domain.task import TaskSpec
from lumberjack.ids import TaskId


def agent(task_id: str, model: str = "claude-opus-5") -> AgentAssignment:
    return AgentAssignment(
        task=TaskSpec(task_id=TaskId(task_id), title=task_id, intent="do the thing"),
        model=model,
    )


def test_a_request_yields_a_validated_graph() -> None:
    request = RunRequest(name="nightly", agents=(agent("a"), agent("b")))

    graph = request.graph()

    assert [item.task_id for item in graph.tasks] == ["a", "b"]
    assert [item.task_id for item in request.task_specs()] == ["a", "b"]


def test_models_are_assignments_not_task_properties() -> None:
    """Which model runs a task is who was asked, not what the work is."""
    request = RunRequest(name="mixed", agents=(agent("a"), agent("b", model="claude-sonnet-5")))

    assert request.models() == {
        "a": "anthropic:claude-opus-5",
        "b": "anthropic:claude-sonnet-5",
    }
    assert not hasattr(request.agents[0].task, "model")


def test_two_agents_cannot_share_a_task() -> None:
    with pytest.raises(ValidationError, match="same task id"):
        RunRequest(name="clash", agents=(agent("a"), agent("a")))


def test_a_request_needs_at_least_one_agent() -> None:
    with pytest.raises(ValidationError):
        RunRequest(name="empty", agents=())


def test_the_picker_and_the_runner_read_the_same_list() -> None:
    """A model offered in the UI that the runner will not accept is a trap."""
    assert known_models(Provider.ANTHROPIC) == MODELS
    assert sum(1 for item in MODELS if item.default) == 1
    for item in MODELS:
        assert item.qualified.startswith("anthropic:")


def test_it_round_trips_through_json() -> None:
    """It crosses a process boundary, so this is the property that matters."""
    request = RunRequest(name="round trip", agents=(agent("a"),))

    restored = RunRequest.model_validate_json(request.model_dump_json())

    assert restored == request
