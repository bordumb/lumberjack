"""Stateful properties of the lease broker.

The grant matrix is small enough to reason about and easy to get subtly wrong under
interleaving, which is exactly what stateful testing is for.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from hypothesis import HealthCheck, settings
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, precondition, rule
from hypothesis.strategies import sampled_from

from lumberjack.adapters.clock import FrozenClock
from lumberjack.adapters.memory_ledger import MemoryLedger
from lumberjack.adapters.projecting import ProjectingLedger
from lumberjack.core.broker import LeaseBroker
from lumberjack.core.projections import Projections
from lumberjack.domain.claim import (
    AccessMode,
    Claim,
    LeaseGranted,
    PathScope,
    modes_conflict,
    scopes_overlap,
)
from lumberjack.domain.events import TaskAssigned
from lumberjack.domain.workstream import StandConfig, Workstream, Worktree
from lumberjack.ids import AgentId, CommitSha, StandId, TaskId, WorkstreamId

STAND = StandId("stand-prop")
AGENTS = ("a", "b", "c")
PATTERNS = ("pkg/core.py", "pkg/**", "docs/guide.md", "docs/**")


class BrokerMachine(RuleBasedStateMachine):
    """Leases are granted, released and expired; the invariants must always hold."""

    def __init__(self) -> None:
        super().__init__()
        self.loop = asyncio.new_event_loop()
        self.clock = FrozenClock()
        self.projections = Projections(stand=STAND)
        self.ledger = ProjectingLedger(
            inner=MemoryLedger(stand=STAND, clock=self.clock), projections=self.projections
        )
        self.broker = LeaseBroker(
            ledger=self.ledger,
            projections=self.projections,
            clock=self.clock,
            config=StandConfig(protected_paths=(".lumberjack/**",)),
        )

    @initialize()
    def register_workstreams(self) -> None:
        for name in AGENTS:
            workstream = Workstream(
                workstream_id=WorkstreamId(f"ws-{name}"),
                stand=STAND,
                agent=AgentId(f"agent-{name}"),
                task=TaskId(f"task-{name}"),
                worktree=Worktree(
                    path=Path(f"/tmp/{name}"),
                    branch=f"lj/{name}",
                    base=CommitSha("0" * 40),
                ),
            )
            self.loop.run_until_complete(
                self.ledger.append(TaskAssigned(task=workstream.task, workstream=workstream))
            )

    @rule(
        agent=sampled_from(AGENTS),
        pattern=sampled_from(PATTERNS),
        mode=sampled_from(list(AccessMode)),
    )
    def request(self, agent: str, pattern: str, mode: AccessMode) -> None:
        self.loop.run_until_complete(
            self.broker.request(
                Claim(
                    claimant=AgentId(f"agent-{agent}"),
                    workstream=WorkstreamId(f"ws-{agent}"),
                    task=TaskId(f"task-{agent}"),
                    scope=PathScope(patterns=(pattern,)),
                    mode=mode,
                    rationale="property test",
                )
            )
        )

    @precondition(lambda self: bool(self.projections.leases))
    @rule()
    def release_one(self) -> None:
        lease_id = next(iter(self.projections.leases))
        self.loop.run_until_complete(self.broker.release(lease_id))

    @rule()
    def pass_time(self) -> None:
        self.clock.advance(self.broker.config.lease_ttl / 3)
        self.loop.run_until_complete(self.broker.expire_due())

    @invariant()
    def exclusive_leases_never_overlap(self) -> None:
        active = self.projections.active_leases(self.clock.now())
        for index, left in enumerate(active):
            for right in active[index + 1 :]:
                if left.workstream == right.workstream:
                    continue
                if scopes_overlap(left.scope, right.scope):
                    assert not modes_conflict(left.mode, right.mode), (
                        f"{left.holder} ({left.mode}) and {right.holder} ({right.mode}) "
                        "hold conflicting overlapping leases"
                    )

    @invariant()
    def expired_leases_are_never_active(self) -> None:
        now = self.clock.now()
        for lease in self.projections.active_leases(now):
            assert lease.expires_at > now

    @invariant()
    def every_lease_belongs_to_a_known_workstream(self) -> None:
        for lease in self.projections.leases.values():
            assert lease.workstream in self.projections.workstreams

    @invariant()
    def granted_leases_never_touch_protected_paths(self) -> None:
        for lease in self.projections.leases.values():
            assert not lease.scope.matches_path(".lumberjack/ledger.db")

    def teardown(self) -> None:
        self.loop.close()


BrokerMachine.TestCase.settings = settings(
    max_examples=40,
    stateful_step_count=25,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
TestBrokerMachine = BrokerMachine.TestCase


def test_granting_is_deterministic_for_the_same_history() -> None:
    """Two identical histories must produce identical lease state."""

    async def play() -> list[tuple[str, str]]:
        projections = Projections(stand=STAND)
        ledger = ProjectingLedger(
            inner=MemoryLedger(stand=STAND, clock=FrozenClock()), projections=projections
        )
        broker = LeaseBroker(
            ledger=ledger,
            projections=projections,
            clock=FrozenClock(),
            config=StandConfig(),
        )
        for name in AGENTS:
            workstream = Workstream(
                workstream_id=WorkstreamId(f"ws-{name}"),
                stand=STAND,
                agent=AgentId(f"agent-{name}"),
                task=TaskId(f"task-{name}"),
                worktree=Worktree(
                    path=Path(f"/tmp/{name}"),
                    branch=f"lj/{name}",
                    base=CommitSha("0" * 40),
                ),
            )
            await ledger.append(TaskAssigned(task=workstream.task, workstream=workstream))

        outcomes: list[tuple[str, str]] = []
        for name, pattern, mode in (
            ("a", "pkg/**", AccessMode.EDIT),
            ("b", "pkg/core.py", AccessMode.EDIT),
            ("c", "pkg/core.py", AccessMode.EXCLUSIVE),
        ):
            decision = await broker.request(
                Claim(
                    claimant=AgentId(f"agent-{name}"),
                    workstream=WorkstreamId(f"ws-{name}"),
                    task=TaskId(f"task-{name}"),
                    scope=PathScope(patterns=(pattern,)),
                    mode=mode,
                    rationale="deterministic",
                )
            )
            outcomes.append((name, decision.kind))
        return outcomes

    first = asyncio.run(play())
    second = asyncio.run(play())

    assert first == second
    assert first == [("a", "granted"), ("b", "granted"), ("c", "queued")]


def test_lease_granted_is_the_only_decision_that_creates_state() -> None:
    async def play() -> None:
        projections = Projections(stand=STAND)
        ledger = ProjectingLedger(
            inner=MemoryLedger(stand=STAND, clock=FrozenClock()), projections=projections
        )
        broker = LeaseBroker(
            ledger=ledger, projections=projections, clock=FrozenClock(), config=StandConfig()
        )
        decision = await broker.request(
            Claim(
                claimant=AgentId("ghost"),
                workstream=WorkstreamId("ws-ghost"),
                task=TaskId("t"),
                scope=PathScope(patterns=("a.py",)),
                mode=AccessMode.EDIT,
                rationale="unknown workstream",
            )
        )
        assert not isinstance(decision, LeaseGranted)
        assert not projections.leases

    asyncio.run(play())
