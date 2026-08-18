"""Arbitration: the two answers to "manager or peers?", behind one interface."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from lumberjack.core.arbitration import (
    FirstWriterWins,
    ForemanRules,
    Hybrid,
    Partition,
    PeerNegotiation,
    policy_for,
)
from lumberjack.core.services import Services
from lumberjack.core.supervisor import Supervisor
from lumberjack.domain.accord import Accord, Defer, Directive, Split
from lumberjack.domain.claim import AccessMode, Claim, PathScope
from lumberjack.domain.conflict import ConflictedFile, ConflictReport, ConflictSource, Severity
from lumberjack.domain.workstream import ArbitrationMode
from lumberjack.ids import ConflictId, WorkstreamId

NOW = datetime(2026, 3, 1, tzinfo=UTC)


def a_conflict(left: WorkstreamId, right: WorkstreamId) -> ConflictReport:
    return ConflictReport(
        conflict_id=ConflictId("cfl-1"),
        between=(left, right),
        source=ConflictSource.MERGE_TREE,
        severity=Severity.BLOCK,
        files=(ConflictedFile(path="pkg/core.py", hunks=1),),
        detected_at=NOW,
        evidence="CONFLICT (content): Merge conflict in pkg/core.py",
    )


def scripted(turns: list[dict[str, Any]]) -> FunctionModel:
    """A negotiator that plays a fixed sequence of moves."""
    state = {"index": 0}

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        index = min(state["index"], len(turns) - 1)
        state["index"] += 1
        return ModelResponse(parts=[ToolCallPart("final_result", turns[index])])

    return FunctionModel(respond)


def ruling_model(resolution: dict[str, Any], rationale: str = "because") -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[ToolCallPart("final_result", {"resolution": resolution, "rationale": rationale})]
        )

    return FunctionModel(respond)


async def setup(services: Services, make_workstream):
    left = await make_workstream("a")
    right = await make_workstream("b")
    for workstream, pattern in ((left, "pkg/core.py"), (right, "pkg/core.py")):
        await services.broker.request(
            Claim(
                claimant=workstream.agent,
                workstream=workstream.workstream_id,
                task=workstream.task,
                scope=PathScope(patterns=(pattern,)),
                mode=AccessMode.EDIT,
                rationale=f"{workstream.agent} needs it",
            )
        )
    supervisor = Supervisor(services=services)
    supervisor.started_at[left.workstream_id] = NOW
    supervisor.started_at[right.workstream_id] = NOW.replace(hour=13)
    return left, right, supervisor


def test_policy_factory_covers_every_mode() -> None:
    expected = {
        ArbitrationMode.PARTITION: Partition,
        ArbitrationMode.FIRST_WRITER_WINS: FirstWriterWins,
        ArbitrationMode.PEER_NEGOTIATION: PeerNegotiation,
        ArbitrationMode.FOREMAN_RULES: ForemanRules,
        ArbitrationMode.HYBRID: Hybrid,
    }
    for mode, kind in expected.items():
        assert isinstance(policy_for(mode), kind)


async def test_partition_defers_the_later_starter(services: Services, make_workstream) -> None:
    left, right, supervisor = await setup(services, make_workstream)

    ruling = await Partition().arbitrate(
        a_conflict(left.workstream_id, right.workstream_id), supervisor._context()
    )

    assert isinstance(ruling, Directive)
    assert isinstance(ruling.resolution, Defer)
    assert ruling.resolution.yielding == right.agent


async def test_peers_can_settle_it_themselves(services: Services, make_workstream) -> None:
    """(b) in the spec: the agents holding the context resolve it, no manager involved."""
    left, right, supervisor = await setup(services, make_workstream)
    split = {
        "kind": "split",
        "assignments": [
            [left.agent, {"kind": "path", "patterns": ["pkg/core.py"]}],
            [right.agent, {"kind": "path", "patterns": ["pkg/caller.py"]}],
        ],
    }
    model = scripted(
        [
            {"say": "I propose we split", "propose": split, "sign": True},
            {"say": "agreed", "sign": True},
        ]
    )

    assert supervisor.negotiator_agent is not None
    with supervisor.negotiator_agent.override(model=model):
        ruling = await PeerNegotiation(escalate_on_failure=False).arbitrate(
            a_conflict(left.workstream_id, right.workstream_id), supervisor._context()
        )

    assert isinstance(ruling, Accord)
    assert isinstance(ruling.resolution, Split)
    assert ruling.signed_by == ruling.participants


async def test_deadlocked_peers_escalate_to_the_foreman(
    services: Services, make_workstream
) -> None:
    """The termination guarantee: two models will agree politely forever otherwise."""
    left, right, supervisor = await setup(services, make_workstream)
    stalling = scripted([{"say": "hmm, let me think about that"}])

    assert supervisor.negotiator_agent is not None
    assert supervisor.arbiter_agent is not None
    with (
        supervisor.negotiator_agent.override(model=stalling),
        supervisor.arbiter_agent.override(
            model=ruling_model(
                {"kind": "defer", "yielding": left.agent, "until": right.workstream_id}
            )
        ),
    ):
        ruling = await Hybrid().arbitrate(
            a_conflict(left.workstream_id, right.workstream_id), supervisor._context()
        )

    assert isinstance(ruling, Directive)
    assert ruling.issued_by == "foreman"


async def test_a_signed_accord_is_actually_enforced(services: Services, make_workstream) -> None:
    """Accords are applied by the core, not honoured on trust."""
    left, right, _ = await setup(services, make_workstream)
    before = services.projections.leases_of(right.workstream_id, services.clock.now())
    assert before

    accord = Accord(
        accord_id="acc-1",
        conflict_id="cfl-1",
        resolution=Defer(yielding=right.agent, until=left.workstream_id),
        signed_by=frozenset({left.agent, right.agent}),
        participants=frozenset({left.agent, right.agent}),
        agreed_at=NOW,
    )
    await services.ledger.append(
        __import__("lumberjack.domain.events", fromlist=["ConflictDetected"]).ConflictDetected(
            report=a_conflict(left.workstream_id, right.workstream_id)
        )
    )

    await services.executor.apply(accord)

    after = services.projections.leases_of(right.workstream_id, services.clock.now())
    assert not after, "the deferring agent's overlapping lease must be released"
    assert services.executor.is_parked(right.workstream_id)


async def test_foreman_rules_directly_when_configured(services: Services, make_workstream) -> None:
    left, right, supervisor = await setup(services, make_workstream)

    assert supervisor.arbiter_agent is not None
    with supervisor.arbiter_agent.override(
        model=ruling_model(
            {
                "kind": "adopt",
                "canonical": left.agent,
                "superseded": right.agent,
                "note": "left's version is better",
            }
        )
    ):
        ruling = await ForemanRules().arbitrate(
            a_conflict(left.workstream_id, right.workstream_id), supervisor._context()
        )

    assert isinstance(ruling, Directive)
    assert ruling.resolution.kind == "adopt"
