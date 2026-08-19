"""The supervisor: the asyncio orchestration that makes a stand run.

Concurrency shape, deliberately flat:

* one task per workstream, running its worker agent;
* one sensor per workstream, watching its worktree;
* one oracle loop, probing pairs on a debounce;
* one train loop, integrating at most one workstream at a time;
* one conflict loop, arbitrating anything at or above ``WARN``.

Nothing here blocks on the foreman.  Workers keep working while conflicts arbitrate,
and arbitration only reaches the foreman when the peers could not settle it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict
from pydantic_ai import Agent
from pydantic_ai.exceptions import UsageLimitExceeded

from lumberjack.adapters.claude_code import ClaudeCodeRunner
from lumberjack.agents.deps import ForemanDeps, NegotiatorDeps, WorkerDeps
from lumberjack.agents.foreman import build_arbiter, build_planner
from lumberjack.agents.models import build_model
from lumberjack.agents.negotiator import build_negotiator
from lumberjack.agents.outputs import (
    ForemanRuling,
    NegotiationOutput,
    Plan,
    TaskBlocked,
    TaskCompleted,
    TaskNeedsSplit,
    WorkerOutput,
    WorkerReport,
)
from lumberjack.agents.runner import PydanticAiRunner
from lumberjack.agents.scout import Scout
from lumberjack.agents.worker import build_worker
from lumberjack.core.arbitration import policy_for
from lumberjack.core.resilience import ArtifactStore, LoopGuard
from lumberjack.core.sensor import WorktreeSensor
from lumberjack.core.services import Services
from lumberjack.core.tasks import record_transition
from lumberjack.domain.accord import (
    Channel,
    ChannelState,
    Directive,
    NegotiationMove,
    Ruling,
)
from lumberjack.domain.conflict import ConflictReport, Severity
from lumberjack.domain.events import (
    ChannelClosed,
    ChannelOpened,
    ComponentFailed,
    ContractFrozen,
    NegotiationTurn,
    StandHalted,
    StandResumed,
    StandStarted,
    TaskAssigned,
    TaskPlanned,
)
from lumberjack.domain.task import (
    Abandoned,
    Assigned,
    AwaitingIntegration,
    Blocked,
    BlockReason,
    Landed,
    Running,
    Task,
    TaskGraph,
    TaskSpec,
)
from lumberjack.domain.workstream import Workstream, Worktree
from lumberjack.ids import (
    AgentId,
    CommitSha,
    StandId,
    TaskId,
    WorkstreamId,
    new_agent_id,
    new_channel_id,
    new_workstream_id,
)
from lumberjack.ports.arbitration import ArbitrationContext, ArbitrationPolicy
from lumberjack.ports.git import GitError
from lumberjack.ports.runner import WorkerRunner
from lumberjack.ports.usage import UsageLedger

__all__ = ["StandOutcome", "Supervisor", "WorkstreamOutcome"]

log = logging.getLogger(__name__)

FOREMAN_ID = AgentId("foreman")


class WorkstreamOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    workstream: WorkstreamId
    agent: AgentId
    task: Task
    branch: str

    @property
    def landed(self) -> bool:
        return isinstance(self.task, Landed)


class StandOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["completed", "partial", "halted", "failed"]
    goal: str
    integration_branch: str
    integration_head: CommitSha | None = None
    workstreams: tuple[WorkstreamOutcome, ...] = ()
    landed: tuple[TaskId, ...] = ()
    blocked: tuple[TaskId, ...] = ()
    conflicts_resolved: int = 0
    duration: timedelta = timedelta(0)
    preserved_worktrees: tuple[str, ...] = ()

    def summary(self) -> str:
        return (
            f"{self.status}: {len(self.landed)} landed, {len(self.blocked)} blocked, "
            f"{self.conflicts_resolved} conflicts resolved on {self.integration_branch}"
        )


@dataclass(slots=True)
class Supervisor:
    services: Services
    policy: ArbitrationPolicy | None = None
    worker_agent: Agent[WorkerDeps, WorkerOutput] | None = None
    runner: WorkerRunner | None = None
    planner_agent: Agent[ForemanDeps, Plan] | None = None
    arbiter_agent: Agent[ForemanDeps, ForemanRuling] | None = None
    negotiator_agent: Agent[NegotiatorDeps, NegotiationOutput] | None = None
    usage: UsageLedger | None = None
    """Where ``Budget.max_total_tokens`` is measured.

    0002 owns the counting and reaches here as ``services.usage``; injecting it
    directly is what lets the enforcement be tested and land independently."""
    artifacts: ArtifactStore | None = None

    stop: asyncio.Event = field(default_factory=asyncio.Event)
    sensors: dict[WorkstreamId, WorktreeSensor] = field(default_factory=dict)
    started_at: dict[WorkstreamId, datetime] = field(default_factory=dict)
    _running: dict[WorkstreamId, asyncio.Task[None]] = field(default_factory=dict)
    _background: list[asyncio.Task[None]] = field(default_factory=list)
    _arbitrating: set[str] = field(default_factory=set)
    _attempted: set[TaskId] = field(default_factory=set)
    """Tasks this process has already given a worker.  Empty in a fresh session."""
    _elapsed: dict[WorkstreamId, float] = field(default_factory=dict)
    """Monotonic start per workstream, for ``Budget.max_wall_clock``."""
    resumed_from: StandId | None = None
    model_overrides: dict[TaskId, str] = field(default_factory=dict)
    """Per-task model, when a run was configured agent by agent."""
    resume_bases: dict[TaskId, CommitSha] = field(default_factory=dict)
    """Per-task starting commits, so a workstream can pick up an earlier stand's branch."""
    _resolved: int = 0

    def __post_init__(self) -> None:
        config = self.services.config
        # A provider that is overloaded should cost a task seconds, not the task, so
        # every agent runs against the configured chain rather than one model.
        model = build_model(config.model, config.fallback_models)
        foreman_model = build_model(config.foreman_model or config.model, config.fallback_models)
        if self.policy is None:
            self.policy = policy_for(config.arbitration)
        if self.worker_agent is None:
            self.worker_agent = build_worker(model)
        if self.runner is None:
            self.runner = self._default_runner()
        if self.planner_agent is None:
            self.planner_agent = build_planner(foreman_model)
        if self.arbiter_agent is None:
            self.arbiter_agent = build_arbiter(foreman_model)
        if self.negotiator_agent is None:
            self.negotiator_agent = build_negotiator(model)
        if self.usage is None:
            # 0002 wires the concrete ledger onto Services; until then, and in tests,
            # it is injected. `getattr` rather than an attribute so the two specs can
            # land in either order without one of them inventing the other's field.
            self.usage = getattr(self.services, "usage", None)
        if self.artifacts is None:
            self.artifacts = ArtifactStore(
                root=config.resolved_state_root() / self.services.stand / "artifacts"
            )

    def _runner_for(self, task: TaskId) -> WorkerRunner:
        """A runner bound to this task's model, when one was chosen for it."""
        assert self.runner is not None
        chosen = self.model_overrides.get(task)
        if chosen is None:
            return self.runner
        if isinstance(self.runner, ClaudeCodeRunner):
            # The claude CLI takes a bare model name, not a provider-qualified one.
            return replace(self.runner, model=chosen.split(":", 1)[-1])
        return PydanticAiRunner(agent=build_worker(chosen))

    def _default_runner(self) -> WorkerRunner:
        config = self.services.config
        if config.worker_runtime == "claude_code":
            return ClaudeCodeRunner(
                repo=config.repo,
                model=config.claude_model,
                permission_mode=config.claude_permission_mode,
                timeout_seconds=config.worker_timeout.total_seconds(),
            )
        assert self.worker_agent is not None
        return PydanticAiRunner(agent=self.worker_agent)

    # -- setup -----------------------------------------------------------------------

    async def prepare(self, goal: str) -> CommitSha:
        services = self.services
        base = await services.git.resolve(services.config.base_ref)
        branch = services.config.integration_branch(services.stand)
        await services.git.create_branch(branch, base, force=True)
        await services.ledger.append(
            StandStarted(
                stand=services.stand,
                goal=goal,
                base=base,
                integration_branch=branch,
                config=services.config,
                pid=os.getpid(),
                resumed_from=self.resumed_from,
            )
        )
        services.projections.repo_map = await Scout(
            git=services.git, indexer=services.indexer
        ).survey(base)
        return base

    async def resume(self) -> StandOutcome:
        """Pick a stand back up: same ledger, same tasks, same branches.

        A stand is a body of work rather than one process lifetime, so continuing it
        adds a session instead of forking a run. Task ids and branch names are
        unchanged, which is what keeps the history of an attempt readable.
        """
        started = self.services.clock.now()
        projections = self.services.projections
        outstanding = projections.outstanding()
        if not outstanding:
            return self._outcome(projections.goal, timedelta(0))

        await self.services.ledger.append(
            StandResumed(
                pid=os.getpid(),
                session=projections.session + 1,
                carried=outstanding,
            )
        )
        assert self.runner is not None
        await self.runner.preflight(self.services)

        await self._requeue_landings()
        graph = TaskGraph(tasks=tuple(projections.specs[task] for task in outstanding))
        base = projections.base or await self.services.git.resolve(self.services.config.base_ref)
        self._background = [
            asyncio.create_task(self._oracle_loop(), name="lj-oracle"),
            asyncio.create_task(self._train_loop(), name="lj-train"),
            asyncio.create_task(self._conflict_loop(), name="lj-conflicts"),
            asyncio.create_task(self._sync_loop(), name="lj-sync"),
        ]
        try:
            await self._schedule(graph, base)
        finally:
            await self._shutdown()
            await self.services.train.drain()
        return self._outcome(projections.title, self.services.clock.now() - started)

    async def _requeue_landings(self) -> None:
        """Work that was waiting to land when the session ended is still waiting.

        The train's queue lives in the process that built it, so a new session has to
        put those entries back or they wait for ever.
        """
        for task in self.services.projections.tasks.values():
            if isinstance(task, AwaitingIntegration):
                await self.services.train.request(task.workstream, task.tip)

    async def plan(self, goal: str) -> TaskGraph:
        assert self.planner_agent is not None
        result = await self.planner_agent.run(
            goal,
            deps=ForemanDeps(
                services=self.services,
                identity=FOREMAN_ID,
                goal=goal,
                repo_map=self.services.projections.repo_map,
            ),
        )
        return await self.adopt(result.output)

    async def adopt(self, plan: Plan) -> TaskGraph:
        """Record a plan -- from the foreman or handed in directly -- as tasks."""
        graph = TaskGraph(tasks=plan.tasks)
        for spec in graph.tasks:
            await self.services.ledger.append(TaskPlanned(spec=spec), actor=FOREMAN_ID)
        for contract in plan.contracts:
            await self.services.ledger.append(ContractFrozen(contract=contract), actor=FOREMAN_ID)
        return graph

    # -- the run loop ----------------------------------------------------------------

    async def run(self, goal: str, *, plan: Plan | None = None) -> StandOutcome:
        started = self.services.clock.now()
        base = await self.prepare(goal)
        assert self.runner is not None
        # Before any worktree exists: if coordination cannot work, do not start.
        await self.runner.preflight(self.services)
        graph = await (self.adopt(plan) if plan is not None else self.plan(goal))

        self._background = [
            asyncio.create_task(self._oracle_loop(), name="lj-oracle"),
            asyncio.create_task(self._train_loop(), name="lj-train"),
            asyncio.create_task(self._conflict_loop(), name="lj-conflicts"),
            asyncio.create_task(self._sync_loop(), name="lj-sync"),
        ]
        try:
            await self._schedule(graph, base)
        finally:
            await self._shutdown()
            await self.services.train.drain()
        return self._outcome(goal, self.services.clock.now() - started)

    async def _shutdown(self, grace: float = 15.0) -> None:
        """Ask the background loops to stop, and give them time to finish.

        Cancelling a loop mid-``git`` leaks the subprocess transport, so the loops
        are asked to leave at their next checkpoint and only cancelled if they overrun.
        """
        self.stop.set()
        if not self._background:
            return
        _, pending = await asyncio.wait(self._background, timeout=grace)
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._background.clear()

    async def _schedule(self, graph: TaskGraph, base: CommitSha) -> None:
        """Launch ready tasks up to ``max_parallel``, until everything is terminal."""
        while not self.stop.is_set():
            if await self._over_token_budget():
                self.stop.set()
                await self._finish_in_flight()
                await self.services.train.drain()
                return
            # One pass, one rule: every ready task that is not finished and has no
            # worker in this process gets one. That covers a first launch, a task the
            # train bounced back, and a task a previous session left behind -- three
            # cases that were three code paths, two of which were wrong.
            completed = self.services.projections.completed_tasks()
            for spec in graph.ready(completed):
                if len(self._running) >= self.services.config.max_parallel:
                    break
                if self._already_handled(spec.task_id):
                    continue
                await self._ensure_worker(spec, base)

            if not self._running:
                # The stand is not over while work is still queued to land. Draining
                # here rather than trusting the periodic train loop is what makes the
                # end of a run deterministic instead of a race with a timer.
                await self.services.train.drain()
                if self.services.train.queue:
                    continue
                if graph.ready(self.services.projections.completed_tasks()) and not all(
                    self._already_handled(spec.task_id) for spec in graph.tasks
                ):
                    continue
                # Nothing running, nothing queued, nothing runnable: the rest is blocked.
                return

            done, _ = await asyncio.wait(
                self._running.values(), return_when=asyncio.FIRST_COMPLETED, timeout=5
            )
            for finished in done:
                for workstream, task in tuple(self._running.items()):
                    if task is finished:
                        self._running.pop(workstream, None)
            await self.services.train.drain()

    async def _ensure_worker(self, spec: TaskSpec, base: CommitSha) -> None:
        """Give this task a worker, reusing the worktree it already has when it has one."""
        self._attempted.add(spec.task_id)
        state = self.services.projections.tasks.get(spec.task_id)
        existing = getattr(state, "workstream", None)
        sensor = self.sensors.get(existing) if existing else None
        workstream = self.services.projections.workstreams.get(existing) if existing else None
        if workstream is not None and sensor is not None and workstream.worktree.path.exists():
            attempts = getattr(state, "attempts", 0)
            self._running[workstream.workstream_id] = asyncio.create_task(
                self._work(workstream, spec, sensor),
                name=f"lj-worker-{workstream.workstream_id}-retry{attempts}",
            )
            return
        await self._launch(spec, base)

    def _already_handled(self, task_id: TaskId) -> bool:
        """Whether this task needs no worker started for it.

        "Not pending" was the wrong test. It is true of every task a previous session
        left behind, so resuming a stand skipped all of them and did nothing at all.
        What actually matters is whether the work is finished, or whether *this*
        process already has a worker on it.
        """
        state = self.services.projections.tasks.get(task_id)
        if state is None:
            return False
        if isinstance(state, Landed | Abandoned):
            return True
        if isinstance(state, AwaitingIntegration):
            return True  # the train owns it; see _requeue_landings
        if isinstance(state, Blocked):
            # A task this process has already worked on and blocked stays blocked.
            # Without this the scheduler hands it a fresh worker on every pass, it
            # blocks again, and the stand spins for ever burning tokens on a task
            # nothing has changed for -- with no limit and nothing in the log saying so.
            # A task blocked by an *earlier* session is not in `_attempted`, so
            # resuming a stand still picks it back up exactly once.
            return task_id in self._attempted
        workstream = getattr(state, "workstream", None)
        return workstream in self._running

    def _all_terminal(self, graph: TaskGraph) -> bool:
        return all(
            isinstance(self.services.projections.tasks.get(spec.task_id), Landed | Blocked)
            for spec in graph.tasks
        )

    async def _branch_tip(self, branch: str) -> CommitSha | None:
        try:
            return await self.services.git.resolve(branch)
        except GitError:
            return None

    async def _launch(self, spec: TaskSpec, base: CommitSha) -> None:
        services = self.services
        workstream_id = new_workstream_id()
        agent_id = new_agent_id("agent")
        branch = services.config.workstream_branch(services.stand, spec.task_id)
        path = services.config.resolved_worktree_root() / workstream_id
        # A branch already exists when this task has been worked on before, in this
        # stand or an earlier one. Continuing it keeps the work as one history.
        existing = await self._branch_tip(branch)
        if existing is not None:
            worktree: Worktree = await services.git.attach_worktree(branch, path)
        else:
            start = self.resume_bases.get(spec.task_id, base)
            worktree = await services.git.add_worktree(branch, start, path)

        workstream = Workstream(
            workstream_id=workstream_id,
            stand=services.stand,
            agent=agent_id,
            task=spec.task_id,
            worktree=worktree,
        )
        await services.ledger.append(
            TaskAssigned(task=spec.task_id, workstream=workstream), actor=agent_id
        )
        assigned = Assigned(spec=spec, agent=agent_id, workstream=workstream_id)
        await record_transition(services.ledger, services.projections, assigned, actor=agent_id)

        sensor = WorktreeSensor(
            workstream=workstream,
            git=services.git,
            ledger=services.ledger,
            projections=services.projections,
            indexer=services.indexer,
            clock=services.clock,
            config=services.config,
            broker=services.broker,
        )
        self.sensors[workstream_id] = sensor
        self.started_at[workstream_id] = services.clock.now()
        self._running[workstream_id] = asyncio.create_task(
            self._work(workstream, spec, sensor), name=f"lj-worker-{workstream_id}"
        )

    async def _work(self, workstream: Workstream, spec: TaskSpec, sensor: WorktreeSensor) -> None:
        services = self.services
        # Carry the attempt count forward. Starting from zero on every launch means a
        # bounced task can never reach the bounce limit, and the train hands it back
        # for ever.
        previous = services.projections.tasks.get(spec.task_id)
        running = Running(
            spec=spec,
            agent=workstream.agent,
            workstream=workstream.workstream_id,
            started_at=services.clock.now(),
            attempts=previous.attempts if isinstance(previous, Running) else 0,
            last_gate=previous.last_gate if isinstance(previous, Running) else None,
        )
        await record_transition(
            services.ledger, services.projections, running, actor=workstream.agent
        )

        # A private stop event per sensor: ``watchfiles`` *sets* the event it is
        # given when its watch ends, so sharing the stand-wide one would let the
        # first worker to finish shut down every other workstream.
        sensor_stop = asyncio.Event()
        watcher = asyncio.create_task(
            sensor.watch(sensor_stop), name=f"lj-sensor-{workstream.workstream_id}"
        )
        try:
            assert self.runner is not None
            runner = self._runner_for(spec.task_id)
            output = await asyncio.wait_for(
                runner.run(workstream, spec, services),
                timeout=self._wall_clock_left(workstream.workstream_id),
            )
            await sensor.scan()
            await self._settle(workstream, spec, output)
        except (TimeoutError, UsageLimitExceeded) as error:
            # A task that ran out of time or out of steps is over budget, not broken.
            # Saying so is the difference between "fix your agent" and "raise the limit".
            await self._block(
                workstream, spec, BlockReason.BUDGET_EXHAUSTED, _budget_detail(error, spec)
            )
        except Exception as error:
            # One agent crashing must not stop the stand, so the task is blocked rather
            # than the exception propagating. The traceback goes to an artifact: it is
            # too big for the ledger and far too useful to discard, which is what
            # `str(error)[:500]` alone used to do.
            ref = self._capture(f"worker-{workstream.workstream_id}", error)
            log.exception("worker %s failed on %s", workstream.agent, spec.task_id)
            await self.services.ledger.append(
                ComponentFailed(
                    component="worker",
                    error=f"{type(error).__name__}: {error}"[:500],
                    giving_up=True,
                    traceback_ref=ref,
                    workstream=workstream.workstream_id,
                ),
                actor=workstream.agent,
            )
            detail = str(error)[:500] + (f" (traceback: {ref})" if ref else "")
            await self._block(workstream, spec, BlockReason.AGENT_ERROR, detail)
        finally:
            sensor_stop.set()
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
            await services.broker.release_all(workstream.workstream_id)

    async def _block(
        self, workstream: Workstream, spec: TaskSpec, reason: BlockReason, detail: str
    ) -> None:
        """Block this task and nothing else.  Its peers keep working and keep landing."""
        current = self.services.projections.tasks.get(spec.task_id)
        if not isinstance(current, Running):
            return
        await record_transition(
            self.services.ledger,
            self.services.projections,
            current.block(reason, detail),
            actor=workstream.agent,
            detail=detail,
        )

    def _capture(self, name: str, error: BaseException) -> str | None:
        if self.artifacts is None:
            return None
        return self.artifacts.write(name, "".join(traceback.format_exception(error)))

    # -- budget ----------------------------------------------------------------------

    def _wall_clock_left(self, workstream: WorkstreamId) -> float:
        """Seconds this task has left of ``Budget.max_wall_clock``.

        Measured on the monotonic clock, not the injected one. ``max_wall_clock`` is a
        limit on how long a task may actually take -- the timeout it feeds is enforced by
        the event loop in real seconds, and pairing that with a clock a test can freeze
        or fast-forward gives an allowance that runs out for reasons nothing did.

        The start is recorded once per workstream, so a task the train bounced back
        continues its allowance rather than being handed a fresh one every attempt.
        """
        budget = self.services.config.budget.max_wall_clock.total_seconds()
        started = self._elapsed.setdefault(workstream, time.monotonic())
        return max(0.001, budget - (time.monotonic() - started))

    async def _over_token_budget(self) -> bool:
        """Whether the stand has spent its ``max_total_tokens``.

        Halting is clean by design: the event is recorded, in-flight work is left to
        finish, and every worktree survives. Killing agents mid-edit to save tokens
        throws away the tokens already spent.
        """
        cap = self.services.config.budget.max_total_tokens
        if cap is None or self.usage is None:
            return False
        spent = self.usage.totals().total_tokens
        if spent < cap:
            return False
        reason = f"token budget exhausted: {spent} of {cap} tokens"
        log.error("%s; halting cleanly and preserving every worktree", reason)
        await self.services.ledger.append(
            StandHalted(
                reason=reason,
                preserved=tuple(
                    item.workstream_id
                    for item in self.services.projections.active_workstreams()
                ),
            )
        )
        return True

    async def _finish_in_flight(self) -> None:
        """Let the workers that are already running reach their own end."""
        if not self._running:
            return
        await asyncio.wait(tuple(self._running.values()))
        self._running.clear()

    async def _settle(self, workstream: Workstream, spec: TaskSpec, report: WorkerReport) -> None:
        services = self.services
        current = services.projections.tasks.get(spec.task_id)
        if not isinstance(current, Running | AwaitingIntegration):
            return
        match report:
            case TaskCompleted():
                if isinstance(current, AwaitingIntegration):
                    return  # already entered the train via request_land
                tip = await services.git.commit_all(
                    workstream.worktree, f"{spec.title} ({spec.task_id})"
                )
                tip = tip or await services.git.head(workstream.worktree)
                awaiting = current.submit(tip)
                await record_transition(
                    services.ledger, services.projections, awaiting, actor=workstream.agent
                )
                await services.train.request(workstream.workstream_id, tip)
            case TaskBlocked():
                if isinstance(current, Running):
                    await record_transition(
                        services.ledger,
                        services.projections,
                        current.block(report.reason, report.needs),
                        actor=workstream.agent,
                        detail=report.needs,
                    )
            case TaskNeedsSplit():
                for proposed in report.proposed:
                    await services.ledger.append(TaskPlanned(spec=proposed), actor=workstream.agent)
                if isinstance(current, Running):
                    await record_transition(
                        services.ledger,
                        services.projections,
                        current.block(BlockReason.NEEDS_HUMAN, report.why),
                        actor=workstream.agent,
                        detail=report.why,
                    )

    # -- background loops ------------------------------------------------------------

    async def _nap(self, delay: timedelta) -> bool:
        """Sleep in slices, returning False as soon as the stand is stopping."""
        slice_size = timedelta(milliseconds=250)
        remaining = delay
        while remaining > timedelta(0):
            if self.stop.is_set():
                return False
            await self.services.clock.sleep(min(slice_size, remaining))
            remaining -= slice_size
        return not self.stop.is_set()

    def _guard(self, component: str) -> LoopGuard:
        """One loop's tolerance for failure.

        Every background loop runs through one of these instead of a
        ``suppress(Exception)``: a failure is logged, recorded as ``ComponentFailed``
        and counted, and the loop stops once it has failed ``loop_failure_limit`` times
        running. A stand that has quietly lost its oracle looks exactly like a healthy
        one, and that is the most expensive lie this system can tell.
        """
        return LoopGuard(
            component=component,
            ledger=self.services.ledger,
            limit=self.services.config.loop_failure_limit,
            artifacts=self.artifacts,
        )

    async def _run_loop(
        self, component: str, delay: timedelta, body: Callable[[], Awaitable[None]]
    ) -> None:
        guard = self._guard(component)
        while not self.stop.is_set():
            if not await self._nap(delay):
                return
            if not await guard.attempt(body):
                log.error("the %s loop has stopped; this stand is degraded", component)
                return

    async def _sync_loop(self) -> None:
        """Fold events written by anyone else -- external MCP agents, ``lj halt``.

        The projecting ledger folds our own writes, but a stand is a multi-process
        system: agents this harness did not spawn append to the same log.
        """
        await self._run_loop("sync", timedelta(seconds=1), self._sync_once)

    async def _sync_once(self) -> None:
        await self.services.projections.hydrate(self.services.ledger)
        if self.services.projections.halted:
            # `lj halt` reaches us as an event, not a call, so cancel the workers
            # here. Relying on the event loop to collect them at teardown happens
            # to work and is not something to depend on.
            self._cancel_workers()
            self.stop.set()

    async def _oracle_loop(self) -> None:
        await self._run_loop("oracle", self.services.config.oracle_debounce, self._oracle_once)

    async def _oracle_once(self) -> None:
        await self.services.oracle.probe_all()
        await self.services.broker.expire_due()

    async def _train_loop(self) -> None:
        await self._run_loop("train", timedelta(seconds=2), self._train_once)

    async def _train_once(self) -> None:
        await self.services.train.run_once()
        await self.services.train.rebase_drifted()

    async def _conflict_loop(self) -> None:
        guard = self._guard("conflicts")
        while not self.stop.is_set():
            if not await self._nap(timedelta(seconds=1)):
                return
            for report in tuple(self.services.projections.conflicts.values()):
                if report.severity < Severity.WARN:
                    continue
                if report.conflict_id in self._arbitrating:
                    continue
                self._arbitrating.add(report.conflict_id)
                if not await guard.attempt(lambda item=report: self._arbitrate(item)):
                    log.error("the conflicts loop has stopped; this stand is degraded")
                    return

    async def _arbitrate(self, report: ConflictReport) -> None:
        assert self.policy is not None
        ruling: Ruling = await self.policy.arbitrate(report, self._context())
        await self.services.executor.apply(ruling)
        self._resolved += 1

    def _context(self) -> ArbitrationContext:
        projections = self.services.projections
        return ArbitrationContext(
            stand=self.services.stand,
            now=self.services.clock.now(),
            agents={
                workstream.workstream_id: workstream.agent
                for workstream in projections.workstreams.values()
            },
            claims={
                workstream_id: tuple(
                    lease.claim
                    for lease in projections.leases_of(workstream_id, self.services.clock.now())
                )
                for workstream_id in projections.workstreams
            },
            started=dict(self.started_at),
            limits=self.services.config.negotiation,
            open_channel=self._open_channel,
            run_negotiation=self._run_negotiation,
            ask_foreman=self._ask_foreman,
        )

    # -- arbitration callbacks -------------------------------------------------------

    async def _open_channel(
        self, report: ConflictReport, participants: frozenset[AgentId]
    ) -> Channel:
        channel_id = new_channel_id()
        await self.services.ledger.append(
            ChannelOpened(
                channel_id=channel_id,
                conflict_id=report.conflict_id,
                participants=participants,
            )
        )
        return Channel(
            channel_id=channel_id,
            conflict_id=report.conflict_id,
            participants=participants,
            limits=self.services.config.negotiation,
            opened_at=self.services.clock.now(),
        )

    async def _run_negotiation(self, channel: Channel, report: ConflictReport) -> Channel:
        """Alternate turns until the peers sign, escalate, or run out of budget."""
        assert self.negotiator_agent is not None
        services = self.services
        while not channel.exhausted(services.clock.now()):
            speaker = channel.whose_turn()
            peer = next(iter(channel.participants - {speaker}), speaker)
            workstream = services.projections.workstream_of(speaker)
            if workstream is None:
                break
            prompt = _negotiation_prompt(report, channel, speaker)
            result = await self.negotiator_agent.run(
                prompt,
                deps=NegotiatorDeps(
                    services=services,
                    identity=speaker,
                    workstream=workstream,
                    peer=peer,
                ),
            )
            move = NegotiationMove(
                by=speaker,
                say=result.output.say,
                propose=result.output.propose,
                sign=result.output.sign,
                at=services.clock.now(),
            )
            await services.ledger.append(
                NegotiationTurn(channel_id=channel.channel_id, move=move), actor=speaker
            )
            channel = channel.with_move(move)
            if channel.on_the_table is not None and channel.signatures == channel.participants:
                channel = channel.closed(ChannelState.SETTLED)
                break
        else:
            channel = channel.closed(ChannelState.EXPIRED)

        await services.ledger.append(
            ChannelClosed(channel_id=channel.channel_id, state=channel.state.value)
        )
        return channel

    async def _ask_foreman(self, report: ConflictReport, channel: Channel | None) -> Directive:
        assert self.arbiter_agent is not None
        services = self.services
        result = await self.arbiter_agent.run(
            _arbitration_prompt(report, channel, services),
            deps=ForemanDeps(
                services=services,
                identity=FOREMAN_ID,
                goal=services.projections.goal,
                repo_map=services.projections.repo_map,
            ),
        )
        return Directive(
            conflict_id=report.conflict_id,
            resolution=result.output.resolution,
            issued_by=FOREMAN_ID,
            rationale=result.output.rationale,
            issued_at=services.clock.now(),
        )

    # -- teardown --------------------------------------------------------------------

    def _cancel_workers(self) -> None:
        """Cancel in-flight workers; each runner is responsible for its own children."""
        for task in self._running.values():
            task.cancel()

    async def halt(self, reason: str = "operator halt") -> None:
        self.stop.set()
        self._cancel_workers()
        preserved = tuple(
            workstream.workstream_id
            for workstream in self.services.projections.active_workstreams()
        )
        await self.services.ledger.append(StandHalted(reason=reason, preserved=preserved))

    def _outcome(self, goal: str, duration: timedelta) -> StandOutcome:
        projections = self.services.projections
        outcomes = tuple(
            WorkstreamOutcome(
                workstream=workstream.workstream_id,
                agent=workstream.agent,
                task=projections.tasks[workstream.task],
                branch=workstream.worktree.branch,
            )
            for workstream in projections.workstreams.values()
            if workstream.task in projections.tasks
        )
        landed = tuple(
            outcome.task.spec.task_id for outcome in outcomes if isinstance(outcome.task, Landed)
        )
        blocked = tuple(
            outcome.task.spec.task_id for outcome in outcomes if isinstance(outcome.task, Blocked)
        )
        if projections.halted:
            status: Literal["completed", "partial", "halted", "failed"] = "halted"
        elif blocked:
            status = "partial"
        elif landed:
            status = "completed"
        else:
            status = "failed"
        return StandOutcome(
            status=status,
            goal=goal,
            integration_branch=projections.integration_branch,
            integration_head=projections.integration_head,
            workstreams=outcomes,
            landed=landed,
            blocked=blocked,
            conflicts_resolved=self._resolved,
            duration=duration,
            # A halted stand preserves everything: it was interrupted, not finished,
            # and which lane happened to land first is not a reason to delete it.
            preserved_worktrees=tuple(
                str(workstream.worktree.path)
                for workstream in projections.workstreams.values()
                if projections.halted or workstream.task not in projections.landed
            ),
        )


def _budget_detail(error: TimeoutError | UsageLimitExceeded, spec: TaskSpec) -> str:
    """Say which limit was hit, so the operator knows which number to change."""
    if isinstance(error, TimeoutError):
        return f"{spec.task_id} exceeded Budget.max_wall_clock"
    return f"{spec.task_id} exceeded Budget.max_steps_per_task: {error}"[:500]


def _negotiation_prompt(report: ConflictReport, channel: Channel, speaker: AgentId) -> str:
    transcript = channel.transcript() or "(you speak first)"
    tabled = channel.on_the_table
    return (
        f"CONFLICT {report.conflict_id} ({report.source.value}, {report.severity.label})\n"
        f"Files: {', '.join(sorted(report.paths)) or '(scope-level, no files yet)'}\n"
        f"Evidence:\n{report.evidence[:1500] or '(none)'}\n\n"
        f"Transcript so far:\n{transcript}\n\n"
        f"On the table: {tabled.kind if tabled else 'nothing yet'}\n"
        f"Turns left: {channel.turns_left}\n\n"
        f"You are {speaker}. Take one turn."
    )


def _arbitration_prompt(report: ConflictReport, channel: Channel | None, services: Services) -> str:
    left, right = report.between
    claims: list[str] = []
    for workstream in (left, right):
        held = services.projections.leases_of(workstream, services.clock.now())
        agent = services.projections.agent_of(workstream)
        rendered = "; ".join(
            f"{lease.scope.describe()} [{lease.mode.value}] -- {lease.claim.rationale}"
            for lease in held
        )
        claims.append(f"{agent} ({workstream}): {rendered or 'no active leases'}")
    transcript = channel.transcript() if channel is not None else "(peers were not consulted)"
    return (
        f"Rule on CONFLICT {report.conflict_id}.\n\n"
        f"Source: {report.source.value}, severity {report.severity.label}\n"
        f"Files: {', '.join(sorted(report.paths)) or '(scope-level)'}\n"
        f"Evidence:\n{report.evidence[:1500] or '(none)'}\n\n"
        "Claims:\n" + "\n".join(f"  {item}" for item in claims) + "\n\n"
        f"Peer negotiation transcript:\n{transcript}\n\n"
        "Issue a binding resolution."
    )
