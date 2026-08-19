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
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict
from pydantic_ai import Agent

from lumberjack.adapters.claude_code import ClaudeCodeRunner
from lumberjack.agents.deps import ForemanDeps, NegotiatorDeps, WorkerDeps
from lumberjack.agents.foreman import build_arbiter, build_planner
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
    ContractFrozen,
    NegotiationTurn,
    StandHalted,
    StandStarted,
    TaskAssigned,
    TaskPlanned,
)
from lumberjack.domain.task import (
    Assigned,
    AwaitingIntegration,
    Blocked,
    BlockReason,
    Landed,
    Pending,
    Running,
    Task,
    TaskGraph,
    TaskSpec,
)
from lumberjack.domain.workstream import Workstream, Worktree
from lumberjack.ids import (
    AgentId,
    CommitSha,
    TaskId,
    WorkstreamId,
    new_agent_id,
    new_channel_id,
    new_workstream_id,
)
from lumberjack.ports.arbitration import ArbitrationContext, ArbitrationPolicy
from lumberjack.ports.runner import WorkerRunner

__all__ = ["StandOutcome", "Supervisor", "WorkstreamOutcome"]

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

    stop: asyncio.Event = field(default_factory=asyncio.Event)
    sensors: dict[WorkstreamId, WorktreeSensor] = field(default_factory=dict)
    started_at: dict[WorkstreamId, datetime] = field(default_factory=dict)
    _running: dict[WorkstreamId, asyncio.Task[None]] = field(default_factory=dict)
    _background: list[asyncio.Task[None]] = field(default_factory=list)
    _arbitrating: set[str] = field(default_factory=set)
    model_overrides: dict[TaskId, str] = field(default_factory=dict)
    """Per-task model, when a run was configured agent by agent."""
    resume_bases: dict[TaskId, CommitSha] = field(default_factory=dict)
    """Per-task starting commits, so a workstream can pick up an earlier stand's branch."""
    _resolved: int = 0

    def __post_init__(self) -> None:
        config = self.services.config
        model = config.model
        foreman_model = config.foreman_model or model
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
            )
        )
        services.projections.repo_map = await Scout(
            git=services.git, indexer=services.indexer
        ).survey(base)
        return base

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
            completed = self.services.projections.completed_tasks()
            for spec in graph.ready(completed):
                if len(self._running) >= self.services.config.max_parallel:
                    break
                if self._already_handled(spec.task_id):
                    continue
                await self._launch(spec, base)

            # A bounced task returns to Running with the failure attached, but nothing
            # re-invokes its worker: the train hands the work back and the supervisor
            # has to pick it up. Without this a bounce silently orphans the task and
            # the stand reports success with the work missing.
            for orphan in self._orphaned(graph):
                await self._relaunch(orphan)

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

    def _orphaned(self, graph: TaskGraph) -> tuple[WorkstreamId, ...]:
        """Tasks left Running with nobody running them -- bounced and handed back."""
        found: list[WorkstreamId] = []
        for spec in graph.tasks:
            state = self.services.projections.tasks.get(spec.task_id)
            if not isinstance(state, Running):
                continue
            if state.workstream in self._running:
                continue
            if len(self._running) >= self.services.config.max_parallel:
                break
            found.append(state.workstream)
        return tuple(found)

    async def _relaunch(self, workstream_id: WorkstreamId) -> None:
        """Re-run a worker in the worktree it already has, keeping its work."""
        workstream = self.services.projections.workstreams.get(workstream_id)
        sensor = self.sensors.get(workstream_id)
        if workstream is None or sensor is None:
            return
        state = self.services.projections.tasks.get(workstream.task)
        if not isinstance(state, Running):
            return
        self._running[workstream_id] = asyncio.create_task(
            self._work(workstream, state.spec, sensor),
            name=f"lj-worker-{workstream_id}-retry{state.attempts}",
        )

    def _already_handled(self, task_id: TaskId) -> bool:
        state = self.services.projections.tasks.get(task_id)
        return state is not None and not isinstance(state, Pending)

    def _all_terminal(self, graph: TaskGraph) -> bool:
        return all(
            isinstance(self.services.projections.tasks.get(spec.task_id), Landed | Blocked)
            for spec in graph.tasks
        )

    async def _launch(self, spec: TaskSpec, base: CommitSha) -> None:
        services = self.services
        workstream_id = new_workstream_id()
        agent_id = new_agent_id("agent")
        branch = services.config.workstream_branch(services.stand, spec.task_id)
        path = services.config.resolved_worktree_root() / workstream_id
        # Resuming starts the worktree on the earlier stand's branch rather than the
        # base, so the agent continues its own work instead of redoing it.
        start = self.resume_bases.get(spec.task_id, base)
        worktree: Worktree = await services.git.add_worktree(branch, start, path)

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
            output = await runner.run(workstream, spec, services)
            await sensor.scan()
            await self._settle(workstream, spec, output)
        except Exception as error:
            current = services.projections.tasks.get(spec.task_id)
            if isinstance(current, Running):
                await record_transition(
                    services.ledger,
                    services.projections,
                    current.block(BlockReason.AGENT_ERROR, str(error)[:500]),
                    actor=workstream.agent,
                    detail=str(error)[:500],
                )
        finally:
            sensor_stop.set()
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
            await services.broker.release_all(workstream.workstream_id)

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

    async def _sync_loop(self) -> None:
        """Fold events written by anyone else -- external MCP agents, ``lj halt``.

        The projecting ledger folds our own writes, but a stand is a multi-process
        system: agents this harness did not spawn append to the same log.
        """
        while not self.stop.is_set():
            if not await self._nap(timedelta(seconds=1)):
                return
            with contextlib.suppress(Exception):
                await self.services.projections.hydrate(self.services.ledger)
            if self.services.projections.halted:
                # `lj halt` reaches us as an event, not a call, so cancel the workers
                # here. Relying on the event loop to collect them at teardown happens
                # to work and is not something to depend on.
                self._cancel_workers()
                self.stop.set()
                return

    async def _oracle_loop(self) -> None:
        while not self.stop.is_set():
            if not await self._nap(self.services.config.oracle_debounce):
                return
            with contextlib.suppress(Exception):
                await self.services.oracle.probe_all()
                await self.services.broker.expire_due()

    async def _train_loop(self) -> None:
        while not self.stop.is_set():
            if not await self._nap(timedelta(seconds=2)):
                return
            with contextlib.suppress(Exception):
                await self.services.train.run_once()
                await self.services.train.rebase_drifted()

    async def _conflict_loop(self) -> None:
        while not self.stop.is_set():
            if not await self._nap(timedelta(seconds=1)):
                return
            for report in tuple(self.services.projections.conflicts.values()):
                if report.severity < Severity.WARN:
                    continue
                if report.conflict_id in self._arbitrating:
                    continue
                self._arbitrating.add(report.conflict_id)
                with contextlib.suppress(Exception):
                    await self._arbitrate(report)

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
            preserved_worktrees=tuple(
                str(workstream.worktree.path)
                for workstream in projections.workstreams.values()
                if workstream.task not in projections.landed
            ),
        )


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
