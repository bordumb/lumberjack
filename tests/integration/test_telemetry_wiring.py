"""Every instrument in 0002 §3.4, emitted by the real thing over a real repository.

The point of this file is the *attribute names*.  A dashboard queries them by string
and a budget check reads them by key, so a rename that no type checker minds is exactly
the failure this suite has to catch.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from lumberjack.adapters.ast_indexer import AstIndexer
from lumberjack.adapters.clock import FrozenClock
from lumberjack.adapters.git_cli import GitCli
from lumberjack.adapters.memory_ledger import MemoryLedger
from lumberjack.adapters.projecting import ProjectingLedger
from lumberjack.adapters.traced import TracedGate, TracedGit, instrumented
from lumberjack.adapters.uv_gate import CommandGate, NullGate
from lumberjack.agents.outputs import Plan
from lumberjack.core.arbitration import PeerNegotiation
from lumberjack.core.projections import Projections
from lumberjack.core.services import Services
from lumberjack.core.supervisor import Supervisor
from lumberjack.domain.claim import AccessMode, Claim, PathScope
from lumberjack.domain.conflict import ConflictedFile, ConflictReport, ConflictSource, Severity
from lumberjack.domain.task import TaskSpec
from lumberjack.domain.workstream import StandConfig
from lumberjack.ids import ConflictId, StandId, TaskId, WorkstreamId
from tests.fakes import RecordingTelemetry

NOW = datetime(2026, 3, 1, tzinfo=UTC)


@pytest.fixture
def telemetry() -> RecordingTelemetry:
    return RecordingTelemetry()


@pytest.fixture
def services(
    repo,
    config: StandConfig,
    clock: FrozenClock,
    stand_id: StandId,
    telemetry: RecordingTelemetry,
) -> Services:
    """The shared ``services`` fixture, wired the way a real stand is: traced.

    Shadowing rather than editing ``tests/conftest.py`` on purpose -- other agents
    depend on that fixture, and ``make_workstream`` picks this one up regardless.
    """
    projections = Projections(stand=stand_id)
    ledger = ProjectingLedger(
        inner=MemoryLedger(stand=stand_id, clock=clock), projections=projections
    )
    git, gate = instrumented(git=GitCli(repo=repo), gate=NullGate(), telemetry=telemetry)
    return Services.wire(
        stand=stand_id,
        config=config,
        clock=clock,
        git=git,
        ledger=ledger,
        indexer=AstIndexer(package_roots=("",)),
        gate=gate,
        projections=projections,
        telemetry=telemetry,
    )


def body(alpha: int = 1, gamma: int = 3) -> str:
    return (
        f"def alpha() -> int:\n    return {alpha}\n\n\n"
        "def beta() -> int:\n    return 2\n\n\n"
        f"def gamma() -> int:\n    return {gamma}\n"
    )


def scripted_worker(script: dict[str, list[tuple[str, dict[str, Any]]]]) -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompt = "".join(str(item) for item in messages if isinstance(item, ModelRequest))
        key = next((name for name in script if name in prompt), None)
        steps = script.get(key or "", [])
        index = sum(isinstance(item, ModelResponse) for item in messages)
        if index < len(steps):
            name, args = steps[index]
            return ModelResponse(parts=[ToolCallPart(name, args)])
        done = next(tool.name for tool in info.output_tools if "Completed" in tool.name)
        return ModelResponse(parts=[ToolCallPart(done, {"summary": "done", "touched": []})])

    return FunctionModel(respond)


def one_task_plan() -> Plan:
    return Plan(
        tasks=(
            TaskSpec(
                task_id=TaskId("task-alpha"),
                title="raise alpha",
                intent="task-alpha: change alpha to return 111",
                acceptance=("alpha returns 111",),
            ),
        ),
        max_parallel=1,
    )


async def a_run(services: Services) -> Supervisor:
    supervisor = Supervisor(services=services)
    model = scripted_worker(
        {
            "task-alpha": [
                ("claim", {"patterns": ["pkg/core.py"], "mode": "edit", "rationale": "alpha"}),
                ("write_file", {"path": "pkg/core.py", "content": body(alpha=111)}),
            ]
        }
    )
    assert supervisor.worker_agent is not None
    with supervisor.worker_agent.override(model=model):
        await supervisor.run("raise alpha", plan=one_task_plan())
    return supervisor


# -- lj.agent.run and lj.tokens --------------------------------------------------------


async def test_a_worker_run_is_spanned_with_the_attributes_0002_names(
    services: Services, telemetry: RecordingTelemetry
) -> None:
    await a_run(services)

    spans = telemetry.spans_named("lj.agent.run")
    assert spans, "a stand that ran a worker and emitted no agent span is untraceable"
    span = spans[0]
    assert set(span.attributes) == {"stand", "workstream", "agent", "task", "outcome"}
    assert span.attributes["task"] == "task-alpha"
    assert span.attributes["outcome"] == "TaskCompleted"
    assert span.ended


async def test_a_function_model_run_yields_non_zero_usage(services: Services) -> None:
    """``Budget`` was dead code because nothing counted.  This is the number that ends that."""
    await a_run(services)

    totals = services.usage.totals()
    assert totals.total_tokens > 0
    assert totals.requests > 0
    assert totals.steps == totals.requests
    assert totals.wall_clock.total_seconds() > 0

    per_workstream = {
        workstream: services.usage.for_workstream(workstream)
        for workstream in services.usage.workstreams()
    }
    assert any(item.total_tokens > 0 for item in per_workstream.values())


async def test_tokens_are_exported_with_the_workstream_agent_and_model(
    services: Services, telemetry: RecordingTelemetry
) -> None:
    await a_run(services)

    assert telemetry.usages, "lj.tokens has nothing to count without a usage record"
    recorded = telemetry.usages[0]
    assert recorded.model == services.config.model
    assert recorded.agent
    assert recorded.usage.total_tokens > 0


# -- lj.train.integration and lj.gate.run ----------------------------------------------


async def test_the_train_counts_every_integration_by_status(
    services: Services, telemetry: RecordingTelemetry
) -> None:
    await a_run(services)

    counted = telemetry.counters_named("lj.train.integration")
    assert counted, "the bounce rate cannot be known if landings are not counted"
    assert all(set(item.attributes) == {"status"} for item in counted)
    assert "landed" in {str(item.attributes["status"]) for item in counted}


async def test_the_gate_is_timed_and_labelled_with_its_first_failure(repo) -> None:
    telemetry = RecordingTelemetry()
    gate = TracedGate(inner=CommandGate(commands=(("false",),)), telemetry=telemetry)
    git = GitCli(repo=repo)
    base = await git.resolve("main")
    worktree = await git.add_worktree("lj/gate-probe", base, repo.parent / "gate-probe")

    report = await gate.run(worktree)

    assert not report.passed
    timed = telemetry.histograms_named("lj.gate.run")[0]
    assert set(timed.attributes) == {"passed", "first_failure"}
    assert timed.attributes["passed"] is False
    assert timed.attributes["first_failure"] == "false"
    assert timed.value > 0


# -- lj.oracle.probe_pair and lj.oracle.conflict ---------------------------------------


async def test_a_real_oracle_probe_records_its_unit_cost(
    services: Services, make_workstream, telemetry: RecordingTelemetry
) -> None:
    """0001_SPEC.md §18.2 asks for "optimal N"; only this measurement can answer it."""
    left = await make_workstream("a")
    right = await make_workstream("b")
    (left.worktree.path / "pkg" / "core.py").write_text(body(alpha=111))
    (right.worktree.path / "pkg" / "core.py").write_text(body(alpha=222))

    await services.oracle.probe_pair(left.workstream_id, right.workstream_id)

    probes = telemetry.histograms_named("lj.oracle.probe_pair")
    assert probes, "the oracle's unit cost is the whole reason this spec exists"
    assert set(probes[0].attributes) == {"clean", "prefiltered"}
    assert probes[0].value > 0

    # And the underlying git call is timed at the boundary, not guessed at.
    assert telemetry.histograms_named("lj.git.merge_tree")


async def test_a_detected_conflict_is_counted_by_source_and_severity(
    services: Services, make_workstream, telemetry: RecordingTelemetry
) -> None:
    left = await make_workstream("a")
    right = await make_workstream("b")
    (left.worktree.path / "pkg" / "core.py").write_text(body(alpha=111))
    (right.worktree.path / "pkg" / "core.py").write_text(body(alpha=222))

    report = await services.oracle.probe_pair(left.workstream_id, right.workstream_id)
    assert report is not None, "two different edits to one line do conflict"

    counted = telemetry.counters_named("lj.oracle.conflict")
    assert counted
    assert set(counted[0].attributes) == {"source", "severity"}
    assert counted[0].attributes["source"] == ConflictSource.MERGE_TREE.value
    assert counted[0].attributes["severity"] == "block"


async def test_a_clean_pair_is_still_timed(
    services: Services, make_workstream, telemetry: RecordingTelemetry
) -> None:
    """A probe that finds nothing costs the same as one that finds something."""
    left = await make_workstream("a")
    right = await make_workstream("b")
    (left.worktree.path / "pkg" / "core.py").write_text(body(alpha=111))
    (right.worktree.path / "pkg" / "caller.py").write_text("def use() -> int:\n    return 9\n")

    await services.oracle.probe_pair(left.workstream_id, right.workstream_id)

    probes = telemetry.histograms_named("lj.oracle.probe_pair")
    assert probes and probes[0].attributes["clean"] is True
    assert not telemetry.counters_named("lj.oracle.conflict")


# -- lj.lease.decision -----------------------------------------------------------------


async def test_every_lease_decision_is_counted_with_its_mode(
    services: Services, make_workstream, telemetry: RecordingTelemetry
) -> None:
    left = await make_workstream("a")
    right = await make_workstream("b")
    for workstream in (left, right):
        await services.broker.request(
            Claim(
                claimant=workstream.agent,
                workstream=workstream.workstream_id,
                task=workstream.task,
                scope=PathScope(patterns=("pkg/core.py",)),
                mode=AccessMode.EDIT,
                rationale="needs it",
            )
        )

    decisions = telemetry.counters_named("lj.lease.decision")
    assert len(decisions) == 2
    assert all(
        set(item.attributes) == {"outcome", "mode", "coexisting", "reason"} for item in decisions
    )
    assert [item.attributes["outcome"] for item in decisions] == ["granted", "granted"]
    assert all(item.attributes["mode"] == "edit" for item in decisions)
    # Two EDIT claims coexist by design; the second one is told about the first.
    assert decisions[1].attributes["coexisting"] == 1


async def test_a_denied_claim_carries_its_reason(
    services: Services, make_workstream, telemetry: RecordingTelemetry
) -> None:
    workstream = await make_workstream("a")
    await services.broker.request(
        Claim(
            claimant=workstream.agent,
            workstream=workstream.workstream_id,
            task=workstream.task,
            scope=PathScope(patterns=(".lumberjack/**",)),
            mode=AccessMode.EDIT,
            rationale="should not be allowed",
        )
    )

    denied = telemetry.counters_named("lj.lease.decision")[0]
    assert denied.attributes["outcome"] == "denied"
    assert denied.attributes["reason"] == "protected_path"


# -- lj.negotiation.turns ---------------------------------------------------------------


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


def scripted_negotiator(turns: list[dict[str, Any]]) -> FunctionModel:
    state = {"index": 0}

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        index = min(state["index"], len(turns) - 1)
        state["index"] += 1
        return ModelResponse(parts=[ToolCallPart("final_result", turns[index])])

    return FunctionModel(respond)


async def test_a_settled_negotiation_records_its_turn_count(
    services: Services, make_workstream, telemetry: RecordingTelemetry
) -> None:
    """The instrument that tests this project's central bet, so it is asserted exactly."""
    left = await make_workstream("a")
    right = await make_workstream("b")
    supervisor = Supervisor(services=services)
    supervisor.started_at[left.workstream_id] = NOW
    supervisor.started_at[right.workstream_id] = NOW

    split = {
        "kind": "split",
        "assignments": [
            [left.agent, {"kind": "path", "patterns": ["pkg/core.py"]}],
            [right.agent, {"kind": "path", "patterns": ["pkg/caller.py"]}],
        ],
    }
    model = scripted_negotiator(
        [
            {"say": "I propose we split", "propose": split, "sign": True},
            {"say": "agreed", "sign": True},
        ]
    )

    assert supervisor.negotiator_agent is not None
    with supervisor.negotiator_agent.override(model=model):
        await PeerNegotiation(escalate_on_failure=False).arbitrate(
            a_conflict(left.workstream_id, right.workstream_id), supervisor._context()
        )

    measured = telemetry.histograms_named("lj.negotiation.turns")
    assert measured, "whether peers settle is the one thing this harness must measure"
    assert set(measured[0].attributes) == {"settled", "escalated", "state"}
    assert measured[0].attributes["settled"] is True
    assert measured[0].attributes["escalated"] is False
    assert measured[0].value == 2

    # Negotiation is spent on the workstreams that negotiated, not on the foreman.
    assert services.usage.for_workstream(left.workstream_id).total_tokens > 0


async def test_an_unsettled_negotiation_is_recorded_as_escalated(
    services: Services, make_workstream, telemetry: RecordingTelemetry
) -> None:
    left = await make_workstream("a")
    right = await make_workstream("b")
    supervisor = Supervisor(services=services)
    supervisor.started_at[left.workstream_id] = NOW
    supervisor.started_at[right.workstream_id] = NOW

    assert supervisor.negotiator_agent is not None
    with supervisor.negotiator_agent.override(
        model=scripted_negotiator([{"say": "hmm, let me think about that"}])
    ):
        await PeerNegotiation(escalate_on_failure=False).arbitrate(
            a_conflict(left.workstream_id, right.workstream_id), supervisor._context()
        )

    measured = telemetry.histograms_named("lj.negotiation.turns")[0]
    assert measured.attributes["settled"] is False
    assert measured.attributes["escalated"] is True


# -- TracedGit -------------------------------------------------------------------------


async def test_traced_git_records_merge_tree_latency_against_a_real_repository(repo) -> None:
    telemetry = RecordingTelemetry()
    git = TracedGit(inner=GitCli(repo=repo), telemetry=telemetry)

    base = await git.resolve("main")
    left = await git.add_worktree("lj/probe-left", base, repo.parent / "probe-left")
    (left.path / "pkg" / "core.py").write_text(body(alpha=111))
    snapshot = await git.snapshot(left)

    result = await git.merge_tree(base, snapshot.commit, base=base)

    assert result.clean
    timed = telemetry.histograms_named("lj.git.merge_tree")
    assert timed, "merge_tree latency is the oracle's unit cost and must be measured"
    assert timed[0].value > 0
    assert set(timed[0].attributes) == {"failed"}
    span = telemetry.spans_named("lj.git.merge_tree")[0]
    assert span.attributes == {"clean": True, "conflicted": 0}
    assert telemetry.histograms_named("lj.git.snapshot"), "sensor cost is measured too"


async def test_a_failing_git_call_is_recorded_on_its_span_and_still_raises(repo) -> None:
    """A wrapper that swallowed the error would be worse than no wrapper at all."""
    from lumberjack.ports.git import GitError

    telemetry = RecordingTelemetry()
    git = TracedGit(inner=GitCli(repo=repo), telemetry=telemetry)

    with pytest.raises(GitError):
        await git.resolve("no-such-ref")

    span = telemetry.spans_named("lj.git.resolve")[0]
    assert span.errors and isinstance(span.errors[0], GitError)
    assert telemetry.histograms_named("lj.git.resolve")[0].attributes["failed"] is True


# -- the untraced default ---------------------------------------------------------------


async def test_a_stand_wired_without_telemetry_still_counts_its_tokens(
    repo, config: StandConfig, clock: FrozenClock, stand_id: StandId
) -> None:
    """``NullTelemetry`` is the default, and usage accounting must not depend on OTel."""
    projections = Projections(stand=stand_id)
    plain = Services.wire(
        stand=stand_id,
        config=config,
        clock=clock,
        git=GitCli(repo=repo),
        ledger=ProjectingLedger(
            inner=MemoryLedger(stand=stand_id, clock=clock), projections=projections
        ),
        indexer=AstIndexer(package_roots=("",)),
        gate=NullGate(),
        projections=projections,
    )

    await a_run(plain)

    assert plain.usage.totals().total_tokens > 0
