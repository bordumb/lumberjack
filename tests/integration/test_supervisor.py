"""End to end, without a language model: plan, work, coordinate, land."""

from __future__ import annotations

from typing import Any

from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from lumberjack.agents.outputs import Plan
from lumberjack.core.services import Services
from lumberjack.core.supervisor import Supervisor
from lumberjack.domain.task import Landed, TaskSpec
from lumberjack.ids import TaskId


def _turns(messages: list[ModelMessage]) -> int:
    return sum(isinstance(item, ModelResponse) for item in messages)


def scripted_worker(script: dict[str, list[tuple[str, dict[str, Any]]]]) -> FunctionModel:
    """Replay a fixed tool sequence per task, keyed by the task id in the prompt."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompt = ""
        for message in messages:
            if isinstance(message, ModelRequest):
                prompt += str(message)
        key = next((name for name in script if name in prompt), None)
        steps = script.get(key or "", [])
        index = _turns(messages)
        if index < len(steps):
            name, args = steps[index]
            return ModelResponse(parts=[ToolCallPart(name, args)])
        done = next(tool.name for tool in info.output_tools if "Completed" in tool.name)
        return ModelResponse(parts=[ToolCallPart(done, {"summary": "done", "touched": []})])

    return FunctionModel(respond)


def a_plan() -> Plan:
    return Plan(
        tasks=(
            TaskSpec(
                task_id=TaskId("task-alpha"),
                title="raise alpha",
                intent="task-alpha: change alpha to return 111",
                acceptance=("alpha returns 111",),
            ),
            TaskSpec(
                task_id=TaskId("task-gamma"),
                title="raise gamma",
                intent="task-gamma: change gamma to return 333",
                acceptance=("gamma returns 333",),
            ),
        ),
        max_parallel=2,
    )


def body(alpha: int = 1, gamma: int = 3) -> str:
    return (
        f"def alpha() -> int:\n    return {alpha}\n\n\n"
        "def beta() -> int:\n    return 2\n\n\n"
        f"def gamma() -> int:\n    return {gamma}\n"
    )


ALPHA_BODY = body(alpha=111)
GAMMA_BODY = body(gamma=333)


async def test_two_agents_work_in_parallel_and_both_land(services: Services) -> None:
    """The whole point, in one test: disjoint edits to one file, both landing."""
    supervisor = Supervisor(services=services)
    model = scripted_worker(
        {
            "task-alpha": [
                ("claim", {"patterns": ["pkg/core.py"], "mode": "edit", "rationale": "alpha"}),
                ("write_file", {"path": "pkg/core.py", "content": ALPHA_BODY}),
            ],
            "task-gamma": [
                ("claim", {"patterns": ["pkg/core.py"], "mode": "edit", "rationale": "gamma"}),
                ("write_file", {"path": "pkg/core.py", "content": GAMMA_BODY}),
            ],
        }
    )

    assert supervisor.worker_agent is not None
    with supervisor.worker_agent.override(model=model):
        outcome = await supervisor.run("raise the constants", plan=a_plan())

    assert outcome.status == "completed", outcome.summary()
    assert sorted(outcome.landed) == ["task-alpha", "task-gamma"]
    assert all(isinstance(item.task, Landed) for item in outcome.workstreams)

    head = services.projections.integration_head
    assert head is not None
    merged = (await services.git.read_blob(head, "pkg/core.py") or b"").decode()
    assert "return 111" in merged
    assert "return 333" in merged, "both agents' work must survive the merge train"


async def test_a_blocked_agent_does_not_stop_the_others(services: Services) -> None:
    supervisor = Supervisor(services=services)
    model = scripted_worker(
        {
            "task-alpha": [
                ("claim", {"patterns": ["pkg/core.py"], "mode": "edit", "rationale": "alpha"}),
                ("write_file", {"path": "pkg/core.py", "content": ALPHA_BODY}),
            ],
            "task-gamma": [
                (
                    "final_result_TaskBlocked",
                    {"reason": "needs_human", "needs": "the spec is ambiguous"},
                ),
            ],
        }
    )

    assert supervisor.worker_agent is not None
    with supervisor.worker_agent.override(model=model):
        outcome = await supervisor.run("raise the constants", plan=a_plan())

    assert outcome.status == "partial"
    assert outcome.landed == ("task-alpha",)
    assert outcome.blocked == ("task-gamma",)
    assert outcome.preserved_worktrees, "unlanded work is never thrown away"


async def test_coordination_is_recorded_in_the_ledger(services: Services) -> None:
    supervisor = Supervisor(services=services)
    model = scripted_worker(
        {
            "task-alpha": [
                ("claim", {"patterns": ["pkg/core.py"], "mode": "edit", "rationale": "alpha"}),
                (
                    "post_note",
                    {
                        "topic": "decisions",
                        "body": "alpha now returns 111",
                        "patterns": ["pkg/core.py"],
                    },
                ),
                ("write_file", {"path": "pkg/core.py", "content": ALPHA_BODY}),
            ],
            "task-gamma": [
                ("who_touches", {"paths": ["pkg/core.py"]}),
                ("claim", {"patterns": ["pkg/core.py"], "mode": "edit", "rationale": "gamma"}),
                ("write_file", {"path": "pkg/core.py", "content": GAMMA_BODY}),
            ],
        }
    )

    assert supervisor.worker_agent is not None
    with supervisor.worker_agent.override(model=model):
        await supervisor.run("raise the constants", plan=a_plan())

    kinds = [item.kind for item in await services.ledger.read()]
    assert "claim_requested" in kinds
    assert "lease_granted" in kinds
    assert "note_posted" in kinds
    assert "worktree_delta" in kinds
    assert "workstream_landed" in kinds
    assert any(note.body.startswith("alpha now") for note in services.projections.notes)
