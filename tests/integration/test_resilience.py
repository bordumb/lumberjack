"""What the harness does when its own parts fail.

Every test here injects the failure at a port, in front of the real adapter, so the
success path in each one is real git doing real work rather than a mock agreeing with
itself.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.function import AgentInfo, FunctionModel
from test_supervisor import a_plan, body, scripted_worker

from fakes import (
    FaultyGit,
    FaultyRunner,
    StubUsage,
    always,
    index_locked,
    not_a_repo,
    rewired,
)
from lumberjack.adapters.ast_indexer import AstIndexer
from lumberjack.adapters.clock import FrozenClock
from lumberjack.adapters.git_cli import GitCli
from lumberjack.adapters.memory_ledger import MemoryLedger
from lumberjack.adapters.projecting import ProjectingLedger
from lumberjack.adapters.retrying import RetryingGit
from lumberjack.adapters.uv_gate import CommandGate, NullGate
from lumberjack.agents.models import build_model
from lumberjack.agents.outputs import WorkerOutput
from lumberjack.agents.runner import PydanticAiRunner
from lumberjack.agents.worker import build_worker
from lumberjack.core.projections import Projections
from lumberjack.core.services import Services
from lumberjack.core.supervisor import Supervisor
from lumberjack.domain.errors import Backoff
from lumberjack.domain.events import ComponentFailed, StandHalted
from lumberjack.domain.task import Blocked, BlockReason, TaskSpec
from lumberjack.domain.workstream import StandConfig, Workstream, Worktree
from lumberjack.ids import AgentId, CommitSha, StandId, TaskId, WorkstreamId
from lumberjack.stand import Stand

ALPHA = TaskId("task-alpha")
GAMMA = TaskId("task-gamma")
ALPHA_BODY = body(alpha=111)
GAMMA_BODY = body(gamma=333)

NO_WAIT = Backoff(attempts=3, base=timedelta(0), cap=timedelta(0))


# -- git retries ---------------------------------------------------------------------


async def test_a_git_index_lock_succeeds_on_retry(repo: Path) -> None:
    """The failure that N worktrees sharing one object store actually produce."""
    inner = FaultyGit(inner=GitCli(repo=repo), faults={"resolve": [index_locked("rev-parse")]})
    git = RetryingGit(inner=inner, backoff=NO_WAIT)

    resolved = await git.resolve("main")

    assert len(resolved) == 40
    assert inner.calls["resolve"] == 2, "one injected failure, then a real call"
    assert git.attempts == 1


async def test_a_terminal_git_failure_is_not_retried(repo: Path) -> None:
    inner = FaultyGit(inner=GitCli(repo=repo), faults={"resolve": always(not_a_repo())})
    git = RetryingGit(inner=inner, backoff=NO_WAIT)

    with pytest.raises(Exception, match="not a git repository"):
        await git.resolve("main")

    assert inner.calls["resolve"] == 1, "a terminal failure is an answer, not a wait"
    assert git.attempts == 0


async def test_retrying_is_bounded(repo: Path) -> None:
    """An unbounded retry is a hang with extra steps."""
    inner = FaultyGit(inner=GitCli(repo=repo), faults={"resolve": always(index_locked())})
    git = RetryingGit(inner=inner, backoff=NO_WAIT)

    with pytest.raises(Exception, match=r"index\.lock"):
        await git.resolve("main")

    assert inner.calls["resolve"] == NO_WAIT.attempts


async def test_a_real_merge_conflict_is_never_retried(repo: Path) -> None:
    """A conflicted merge is an answer.  Asking git again wastes a merge and lies.

    ``merge_tree`` reports a conflict as a *value*, never an exception, which is what
    makes this structurally impossible rather than merely avoided.
    """
    inner = FaultyGit(inner=GitCli(repo=repo))
    git = RetryingGit(inner=inner, backoff=NO_WAIT)

    base = await git.resolve("main")
    left = await _branch_with(git, repo, "left", base, body(alpha=7))
    right = await _branch_with(git, repo, "right", base, body(alpha=9))

    merged = await git.merge_tree(left, right, base=base)

    assert merged.clean is False, "the fixture has to actually conflict"
    assert inner.calls["merge_tree"] == 1
    assert git.attempts == 0


async def _branch_with(
    git: RetryingGit, repo: Path, branch: str, base: CommitSha, content: str
) -> CommitSha:
    path = repo.parent / f"wt-{branch}"
    worktree: Worktree = await git.add_worktree(branch, base, path)
    (path / "pkg" / "core.py").write_text(content)
    tip = await git.commit_all(worktree, f"{branch} edit")
    assert tip is not None
    return tip


# -- background loops ----------------------------------------------------------------


async def test_an_oracle_that_always_raises_stops_its_loop_and_says_so(
    repo: Path, config: StandConfig, clock: FrozenClock, stand_id: StandId
) -> None:
    """The worst failure this system has: degrading quietly into an uncoordinated swarm.

    Without this the stand keeps running, conflicts are never detected, and nothing
    anywhere says so.
    """
    projections = Projections(stand=stand_id)
    ledger = ProjectingLedger(
        inner=MemoryLedger(stand=stand_id, clock=clock), projections=projections
    )
    services = Services.wire(
        stand=stand_id,
        config=config.model_copy(
            update={"loop_failure_limit": 2, "oracle_debounce": timedelta(milliseconds=1)}
        ),
        clock=clock,
        git=FaultyGit(inner=GitCli(repo=repo), faults={"snapshot": always(index_locked())}),
        ledger=ledger,
        indexer=AstIndexer(package_roots=("",)),
        gate=NullGate(),
        projections=projections,
    )
    projections.workstreams[WorkstreamId("ws-a")] = _a_workstream(stand_id, repo)
    supervisor = Supervisor(services=services)

    await asyncio.wait_for(supervisor._oracle_loop(), timeout=5)

    reported = [
        item.payload for item in await ledger.read() if isinstance(item.payload, ComponentFailed)
    ]
    assert [item.consecutive for item in reported] == [1, 2], (
        "consecutive failures are counted, not just noticed"
    )
    assert reported[-1].component == "oracle"
    assert reported[-1].giving_up is True, "a loop that cannot work stops rather than spins"
    assert projections.degraded["oracle"].giving_up is True

    assert supervisor.artifacts is not None
    ref = reported[-1].traceback_ref
    assert ref is not None
    saved = supervisor.artifacts.read(ref)
    assert saved is not None and "index.lock" in saved


async def test_a_degraded_oracle_appears_in_the_awareness_digest(
    services: Services, make_workstream
) -> None:
    """An agent with no conflict detection deserves to know; it changes what it does."""
    workstream = await make_workstream("alpha")
    assert "HARNESS DEGRADED" not in services.digest.render(workstream.workstream_id)

    await services.ledger.append(
        ComponentFailed(
            component="oracle", error="GitError: index.lock", consecutive=3, giving_up=True
        )
    )

    rendered = services.digest.render(workstream.workstream_id)
    assert "HARNESS DEGRADED" in rendered
    assert "oracle" in rendered
    assert "check_merge()" in rendered, "say what to do instead, not only what broke"


async def test_a_single_failure_does_not_shout_at_every_agent(
    services: Services, make_workstream
) -> None:
    """Digest space is every agent's context.  Only a stopped loop earns a line in it."""
    workstream = await make_workstream("alpha")
    await services.ledger.append(
        ComponentFailed(component="oracle", error="one blip", consecutive=1, giving_up=False)
    )

    assert "HARNESS DEGRADED" not in services.digest.render(workstream.workstream_id)
    assert services.projections.degraded["oracle"].consecutive == 1, "still in the ledger"


# -- workers -------------------------------------------------------------------------


async def test_a_worker_that_raises_blocks_only_its_own_task(services: Services) -> None:
    """One agent crashing must not stop the stand -- and must not vanish either."""
    supervisor = Supervisor(services=services)
    assert supervisor.worker_agent is not None
    supervisor.runner = FaultyRunner(
        inner=PydanticAiRunner(agent=supervisor.worker_agent),
        explode_on={GAMMA: RuntimeError("the runner fell over")},
    )
    model = scripted_worker(
        {
            "task-alpha": [
                ("claim", {"patterns": ["pkg/core.py"], "mode": "edit", "rationale": "alpha"}),
                ("write_file", {"path": "pkg/core.py", "content": ALPHA_BODY}),
            ]
        }
    )

    with supervisor.worker_agent.override(model=model):
        outcome = await supervisor.run("raise the constants", plan=a_plan())

    assert outcome.landed == (ALPHA,), "the healthy peer still lands"
    assert outcome.blocked == (GAMMA,)

    blocked = services.projections.tasks[GAMMA]
    assert isinstance(blocked, Blocked)
    assert blocked.reason is BlockReason.AGENT_ERROR
    assert "the runner fell over" in blocked.detail


async def test_a_crashed_worker_keeps_its_traceback(services: Services) -> None:
    """``str(error)[:500]`` threw away the only copy of *why* it happened."""
    supervisor = Supervisor(services=services)
    assert supervisor.worker_agent is not None
    supervisor.runner = FaultyRunner(
        inner=PydanticAiRunner(agent=supervisor.worker_agent),
        explode_on={ALPHA: ValueError("deep inside"), GAMMA: ValueError("deep inside")},
    )

    with supervisor.worker_agent.override(model=scripted_worker({})):
        await supervisor.run("raise the constants", plan=a_plan())

    reported = [
        item.payload
        for item in await services.ledger.read()
        if isinstance(item.payload, ComponentFailed) and item.payload.component == "worker"
    ]
    assert reported, "a worker crash is a component failure, and lands in the ledger"

    assert supervisor.artifacts is not None
    ref = reported[0].traceback_ref
    assert ref is not None
    saved = supervisor.artifacts.read(ref)
    assert saved is not None
    assert "Traceback" in saved
    assert "ValueError: deep inside" in saved


# -- budget --------------------------------------------------------------------------


async def test_a_task_over_its_wall_clock_is_blocked_as_over_budget(services: Services) -> None:
    supervisor = Supervisor(
        services=rewired(services, budget=_budget(services, max_wall_clock=timedelta(seconds=0.05)))
    )
    supervisor.runner = _SlowRunner()

    outcome = await supervisor.run("go slowly", plan=a_plan())

    assert outcome.status == "partial"
    blocked = services.projections.tasks[ALPHA]
    assert isinstance(blocked, Blocked)
    assert blocked.reason is BlockReason.BUDGET_EXHAUSTED
    assert "max_wall_clock" in blocked.detail


async def test_a_task_over_its_step_limit_is_blocked_as_over_budget(services: Services) -> None:
    """``BlockReason.BUDGET_EXHAUSTED`` existed and was unreachable.  Now it is reached."""
    budget = _budget(services, max_steps_per_task=3)
    supervisor = Supervisor(services=rewired(services, budget=budget))

    def never_finishes(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        _ = messages, info
        return ModelResponse(parts=[ToolCallPart("list_dir", {"path": "."})])

    assert supervisor.worker_agent is not None
    with supervisor.worker_agent.override(model=FunctionModel(never_finishes)):
        outcome = await supervisor.run("loop for ever", plan=a_plan())

    assert outcome.status == "partial"
    blocked = services.projections.tasks[ALPHA]
    assert isinstance(blocked, Blocked)
    assert blocked.reason is BlockReason.BUDGET_EXHAUSTED
    assert "max_steps_per_task" in blocked.detail


async def test_a_stand_over_its_token_budget_halts_and_preserves_every_worktree(
    services: Services,
) -> None:
    """Halting cleanly: the event is recorded, and nothing an agent produced is destroyed."""
    supervisor = Supervisor(
        services=rewired(services, budget=_budget(services, max_total_tokens=1500)),
        usage=StubUsage(per_call=1000),
    )
    model = scripted_worker(
        {
            "task-alpha": [("write_file", {"path": "pkg/core.py", "content": ALPHA_BODY})],
            "task-gamma": [("write_file", {"path": "pkg/other.py", "content": "x = 1\n"})],
        }
    )

    assert supervisor.worker_agent is not None
    with supervisor.worker_agent.override(model=model):
        outcome = await supervisor.run("raise the constants", plan=a_plan())

    assert outcome.status == "halted"
    halts = [
        item.payload
        for item in await services.ledger.read()
        if isinstance(item.payload, StandHalted)
    ]
    assert halts, "a budget halt is an event, not a log line"
    assert "token budget exhausted" in halts[-1].reason

    live = services.projections.workstreams
    assert len(outcome.preserved_worktrees) == len(live), "every worktree survives a halt"
    assert all(item.worktree.path.exists() for item in live.values())


async def test_an_unset_token_budget_never_halts(services: Services) -> None:
    """``max_total_tokens`` is opt-in; leaving it unset must not stop a healthy stand."""
    supervisor = Supervisor(services=services, usage=StubUsage(fixed=10**9))

    assert services.config.budget.max_total_tokens is None
    assert await supervisor._over_token_budget() is False


# -- cancellation --------------------------------------------------------------------


async def test_one_workstream_finishing_does_not_shut_down_the_others(
    services: Services,
) -> None:
    """``watchfiles`` *sets* the stop event it is handed when its watch ends.

    Sharing the stand-wide event therefore let the first worker to finish shut down
    every other workstream, and no test would have caught it.  Two lanes both landing
    is the assertion that would have.
    """
    supervisor = Supervisor(services=services)
    model = scripted_worker(
        {
            "task-alpha": [("write_file", {"path": "pkg/core.py", "content": ALPHA_BODY})],
            "task-gamma": [("write_file", {"path": "pkg/other.py", "content": "x = 1\n"})],
        }
    )

    assert supervisor.worker_agent is not None
    with supervisor.worker_agent.override(model=model):
        outcome = await supervisor.run("touch two files", plan=a_plan())

    assert sorted(outcome.landed) == [ALPHA, GAMMA]


async def test_shutdown_asks_the_loops_to_leave_rather_than_cancelling_mid_git(
    services: Services,
) -> None:
    """Cancelling a loop mid-``git`` leaks the subprocess transport, so we ask first."""
    supervisor = Supervisor(services=services)
    left_cleanly = asyncio.Event()

    async def loop() -> None:
        await supervisor.stop.wait()
        await asyncio.sleep(0.05)  # a checkpoint that takes a moment, as a git call does
        left_cleanly.set()

    supervisor._background = [asyncio.create_task(loop(), name="lj-fake")]
    await supervisor._shutdown(grace=2.0)

    assert left_cleanly.is_set(), "the loop reached its own checkpoint"
    assert supervisor._background == []


async def test_shutdown_still_cancels_a_loop_that_overruns(services: Services) -> None:
    supervisor = Supervisor(services=services)

    async def stuck() -> None:
        await asyncio.sleep(60)

    task: asyncio.Task[None] = asyncio.create_task(stuck(), name="lj-stuck")
    supervisor._background = [task]
    await supervisor._shutdown(grace=0.05)

    assert task.cancelled()


async def test_one_ledger_subscriber_leaving_does_not_stop_another(
    clock: FrozenClock, stand_id: StandId
) -> None:
    """The same shape as the ``watchfiles`` bug, audited: private signal per subscriber.

    ``MemoryLedger`` hands each subscriber its own event and drops it on the way out. If
    they shared one, the first consumer to finish would silently deafen the rest.
    """
    ledger = MemoryLedger(stand=stand_id, clock=clock)
    seen: list[str] = []

    async def consume(label: str, limit: int) -> None:
        async for envelope in ledger.subscribe():
            seen.append(f"{label}:{envelope.seq}")
            if len([item for item in seen if item.startswith(label)]) >= limit:
                return

    short = asyncio.create_task(consume("short", 1))
    long_lived = asyncio.create_task(consume("long", 2))
    await asyncio.sleep(0)

    await ledger.append(ComponentFailed(component="probe", error="one"))
    await asyncio.wait_for(short, timeout=2)
    await ledger.append(ComponentFailed(component="probe", error="two"))

    await asyncio.wait_for(long_lived, timeout=2)
    assert seen.count("long:1") == 1
    assert "long:2" in seen, "the surviving subscriber still receives events"


async def test_closing_the_ledger_releases_every_subscriber(
    clock: FrozenClock, stand_id: StandId
) -> None:
    """A subscriber that outlives its ledger is a task that never finishes at shutdown."""
    ledger = MemoryLedger(stand=stand_id, clock=clock)

    async def consume() -> int:
        count = 0
        async for _ in ledger.subscribe():
            count += 1
        return count

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    await ledger.close()

    assert await asyncio.wait_for(task, timeout=2) == 0


# -- the gate ------------------------------------------------------------------------


async def test_one_gate_timeout_is_transient_and_two_is_a_hung_suite(repo: Path) -> None:
    """A timeout is transient once; twice running it is a hung suite, and worth saying."""
    gate = CommandGate(
        commands=(("sleep", "30"),),
        timeout=timedelta(milliseconds=50),
    )
    worktree = Worktree(path=repo, branch="main", base=CommitSha("0" * 40))

    first = await gate.run(worktree)
    assert gate.hung(worktree) == ()
    assert "loaded" in first.checks[0].log_excerpt

    second = await gate.run(worktree)
    assert gate.hung(worktree) == ("sleep",)
    assert "hung suite" in second.checks[0].log_excerpt
    assert "not a slow machine" in second.checks[0].log_excerpt


async def test_a_gate_that_recovers_forgets_it_ever_timed_out(repo: Path) -> None:
    slow = CommandGate(commands=(("sleep", "30"),), timeout=timedelta(milliseconds=50))
    worktree = Worktree(path=repo, branch="main", base=CommitSha("0" * 40))
    await slow.run(worktree)
    assert slow.timeouts

    quick = CommandGate(commands=(("true",),), timeout=timedelta(seconds=30))
    quick.timeouts.update(slow.timeouts)
    await quick.run(worktree)

    assert quick.hung(worktree) == ()


# -- agent-facing errors -------------------------------------------------------------


async def test_a_refusal_tells_the_agent_what_to_do_instead(services: Services) -> None:
    """``ModelRetry`` text is read by a model deciding what to do next.

    A message that only names the problem sends it round the same loop again; every one
    of these has to name the alternative as well.
    """
    from pydantic_ai import ModelRetry

    from lumberjack.agents.workspace import _resolve

    context = _a_run_context(services)
    with pytest.raises(ModelRetry) as escaped:
        _resolve(context, "../../etc/passwd")
    assert "not touched" in str(escaped.value)
    assert "message them" in str(escaped.value)

    with pytest.raises(ModelRetry) as guarded:
        _resolve(context, ".lumberjack/state.db")
    assert "request_land" in str(guarded.value)


def _a_run_context(services: Services):
    from pydantic_ai import RunContext
    from pydantic_ai.models.test import TestModel
    from pydantic_ai.usage import RunUsage

    from lumberjack.agents.deps import WorkerDeps
    from lumberjack.domain.task import TaskSpec

    spec = TaskSpec(task_id=ALPHA, title="t", intent="i")
    deps = WorkerDeps(
        services=services,
        identity=AgentId("agent-a"),
        workstream=WorkstreamId("ws-a"),
        task=spec,
        worktree=Worktree(
            path=services.config.repo, branch="main", base=CommitSha("0" * 40)
        ),
    )
    return RunContext(deps=deps, model=TestModel(), usage=RunUsage())


# -- helpers -------------------------------------------------------------------------


class _SlowRunner:
    """A runner that never returns, so only the wall-clock budget can stop it."""

    name = "slow"

    async def preflight(self, services: Services) -> None:
        _ = services

    async def run(
        self, workstream: Workstream, spec: TaskSpec, services: Services
    ) -> WorkerOutput:
        _ = workstream, spec, services
        await asyncio.sleep(30)
        raise AssertionError("the wall-clock budget should have cut this off")


def _budget(services: Services, **limits: object):
    return services.config.budget.model_copy(update=limits)


def _a_workstream(stand: StandId, repo: Path) -> Workstream:
    return Workstream(
        workstream_id=WorkstreamId("ws-a"),
        stand=stand,
        agent=AgentId("agent-a"),
        task=TaskId("task-a"),
        worktree=Worktree(path=repo, branch="main", base=CommitSha("0" * 40)),
    )


# -- model fallback ------------------------------------------------------------------


def test_no_fallbacks_leaves_the_model_name_alone() -> None:
    """``Agent(name, defer_model_check=True)`` resolves lazily; a chain resolves eagerly.

    Building a ``FallbackModel`` unconditionally would mean every stand needed working
    credentials for every provider before it could construct an agent.
    """
    assert build_model("anthropic:claude-opus-5") == "anthropic:claude-opus-5"
    assert build_model(None) is None


async def test_provider_overload_degrades_to_the_next_model_instead_of_blocking(
    services: Services,
) -> None:
    """A provider returning 529 should cost a task seconds, not the task."""
    calls: list[str] = []

    def overloaded(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        _ = messages, info
        calls.append("primary")
        raise ModelHTTPError(status_code=529, model_name="primary", body="overloaded")

    def healthy(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        _ = messages
        calls.append("fallback")
        done = next(tool.name for tool in info.output_tools if "Completed" in tool.name)
        return ModelResponse(parts=[ToolCallPart(done, {"summary": "done", "touched": []})])

    chain = build_model(FunctionModel(overloaded), [FunctionModel(healthy)])
    assert isinstance(chain, FallbackModel)

    supervisor = Supervisor(services=services, worker_agent=build_worker(chain))
    outcome = await supervisor.run("write nothing much", plan=a_plan())

    assert calls[0] == "primary" and "fallback" in calls
    assert outcome.blocked == (), "provider overload must not block a task"
    assert sorted(outcome.landed) == [ALPHA, GAMMA]


def test_the_fallback_chain_comes_from_configuration(services: Services) -> None:
    """Which models to fall through to is an operator's decision, not a constant."""
    config = services.config.model_copy(update={"fallback_models": ("anthropic:claude-sonnet-5",)})
    assert config.fallback_models == ("anthropic:claude-sonnet-5",)


# -- teardown ------------------------------------------------------------------------


async def test_close_says_whether_a_worktree_was_kept_or_could_not_be_removed(
    repo: Path, config: StandConfig, clock: FrozenClock, stand_id: StandId
) -> None:
    """Two very different things used to arrive in one undifferentiated list.

    "Kept because its work never landed" is the design working. "Kept because removal
    failed" is a directory the operator now owns, and it was invisible.
    """
    stand = await Stand.create(
        config,
        stand_id=stand_id,
        git=FaultyGit(
            inner=GitCli(repo=repo),
            faults={"remove_worktree": always(not_a_repo("worktree remove"))},
        ),
        ledger=MemoryLedger(stand=stand_id, clock=clock),
        gate=NullGate(),
        clock=clock,
    )
    projections = stand.services.projections
    landed_task, open_task = TaskId("task-landed"), TaskId("task-open")
    projections.workstreams[WorkstreamId("ws-landed")] = _a_named_workstream(
        stand_id, repo / "landed", "ws-landed", landed_task
    )
    projections.workstreams[WorkstreamId("ws-open")] = _a_named_workstream(
        stand_id, repo / "open", "ws-open", open_task
    )
    projections.landed[landed_task] = CommitSha("a" * 40)

    await stand.close()

    reasons = {item.reason: item for item in stand.preserved_worktrees}
    assert set(reasons) == {"unlanded", "cleanup_failed"}
    assert reasons["unlanded"].path.endswith("open")
    assert reasons["cleanup_failed"].path.endswith("landed")
    assert "not a git repository" in reasons["cleanup_failed"].detail
    assert stand.cleanup_failures == (reasons["cleanup_failed"],)
    assert "delete by hand" in reasons["cleanup_failed"].render()
    assert set(stand.preserved) == {item.path for item in reasons.values()}


def _a_named_workstream(stand: StandId, path: Path, name: str, task: TaskId) -> Workstream:
    return Workstream(
        workstream_id=WorkstreamId(name),
        stand=stand,
        agent=AgentId(f"agent-{name}"),
        task=task,
        worktree=Worktree(path=path, branch=name, base=CommitSha("0" * 40)),
    )
