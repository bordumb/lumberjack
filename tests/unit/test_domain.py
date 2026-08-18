"""The domain model: scopes, the task state machine, accords, contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from lumberjack.domain.accord import Accord, Adopt, Defer, Split
from lumberjack.domain.claim import (
    AccessMode,
    PathScope,
    SymbolScope,
    modes_conflict,
    scopes_overlap,
)
from lumberjack.domain.contract import Contract
from lumberjack.domain.gate import CheckOutcome, CheckResult, GateReport
from lumberjack.domain.symbols import SymbolRef, signature_digest
from lumberjack.domain.task import (
    Assigned,
    AwaitingIntegration,
    Landed,
    Pending,
    Running,
    TaskGraph,
    TaskSpec,
)
from lumberjack.ids import (
    AccordId,
    AgentId,
    CommitSha,
    ConflictId,
    ContractId,
    TaskId,
    WorkstreamId,
)

NOW = datetime(2026, 3, 1, tzinfo=UTC)


def symbol(name: str, signature: str = "def f() -> int") -> SymbolRef:
    return SymbolRef(module="pkg.core", qualname=name, path="pkg/core.py", signature=signature)


def spec(task_id: str = "task-1", depends_on: frozenset[TaskId] = frozenset()) -> TaskSpec:
    return TaskSpec(task_id=TaskId(task_id), title="t", intent="do it", depends_on=depends_on)


class TestScopes:
    def test_path_scopes_overlap_conservatively(self) -> None:
        assert scopes_overlap(
            PathScope(patterns=("src/**/*.py",)), PathScope(patterns=("src/core/a.py",))
        )
        assert not scopes_overlap(
            PathScope(patterns=("src/**",)), PathScope(patterns=("tests/**",))
        )

    def test_symbol_scopes_distinguish_within_a_file(self) -> None:
        """Two agents in one file, different functions, is the case that must work."""
        left = SymbolScope(symbols=(symbol("alpha"),))
        right = SymbolScope(symbols=(symbol("beta"),))
        # Same file, so it is a neighbourhood -- but the oracle decides, not this.
        assert scopes_overlap(left, right)
        assert scopes_overlap(left, left)

    def test_symbol_scope_against_path_scope(self) -> None:
        assert scopes_overlap(
            SymbolScope(symbols=(symbol("alpha"),)), PathScope(patterns=("pkg/*.py",))
        )
        assert not scopes_overlap(
            SymbolScope(symbols=(symbol("alpha"),)), PathScope(patterns=("other/**",))
        )


class TestModes:
    @pytest.mark.parametrize(
        ("held", "wanted", "conflicts"),
        [
            (AccessMode.EDIT, AccessMode.EDIT, False),
            (AccessMode.READ, AccessMode.EDIT, False),
            (AccessMode.EDIT, AccessMode.EXCLUSIVE, True),
            (AccessMode.EXCLUSIVE, AccessMode.READ, True),
            (AccessMode.EXCLUSIVE, AccessMode.EXCLUSIVE, True),
        ],
    )
    def test_grant_matrix(self, held: AccessMode, wanted: AccessMode, conflicts: bool) -> None:
        assert modes_conflict(held, wanted) is conflicts

    def test_two_edits_coexist_by_design(self) -> None:
        """The core wager: git merges different hunks, so let both proceed."""
        assert not modes_conflict(AccessMode.EDIT, AccessMode.EDIT)


class TestTaskLifecycle:
    def test_happy_path_transitions(self) -> None:
        pending = Pending(spec=spec())
        assigned = pending.assign(AgentId("a"), WorkstreamId("ws"))
        assert isinstance(assigned, Assigned)
        running = assigned.start(NOW)
        assert isinstance(running, Running)
        awaiting = running.submit(CommitSha("abc1234"))
        assert isinstance(awaiting, AwaitingIntegration)
        landed = awaiting.land(CommitSha("def5678"), NOW)
        assert isinstance(landed, Landed)
        assert landed.spec.task_id == "task-1"

    def test_bounce_returns_to_the_same_worker_with_context(self) -> None:
        awaiting = (
            Pending(spec=spec())
            .assign(AgentId("a"), WorkstreamId("ws"))
            .start(NOW)
            .submit(CommitSha("abc1234"))
        )
        report = GateReport(
            checks=(
                CheckResult(
                    name="pytest",
                    command=("pytest",),
                    outcome=CheckOutcome.FAILED,
                    exit_code=1,
                    log_excerpt="1 failed",
                ),
            )
        )
        bounced = awaiting.bounce(report)
        assert isinstance(bounced, Running)
        assert bounced.agent == awaiting.agent
        assert bounced.attempts == 1
        assert bounced.last_gate is not None
        assert not bounced.last_gate.passed

    def test_states_are_frozen(self) -> None:
        running = Pending(spec=spec()).assign(AgentId("a"), WorkstreamId("ws")).start(NOW)
        with pytest.raises(ValidationError):
            running.__setattr__("attempts", 4)


class TestTaskGraph:
    def test_rejects_cycles(self) -> None:
        with pytest.raises(ValidationError, match="cycle"):
            TaskGraph(
                tasks=(
                    spec("a", depends_on=frozenset({TaskId("b")})),
                    spec("b", depends_on=frozenset({TaskId("a")})),
                )
            )

    def test_rejects_unknown_dependencies(self) -> None:
        with pytest.raises(ValidationError, match="unknown"):
            TaskGraph(tasks=(spec("a", depends_on=frozenset({TaskId("ghost")})),))

    def test_layers_and_ready(self) -> None:
        graph = TaskGraph(
            tasks=(
                spec("a"),
                spec("b", depends_on=frozenset({TaskId("a")})),
                spec("c", depends_on=frozenset({TaskId("a")})),
            )
        )
        layers = graph.topological_layers()
        assert [item.task_id for item in layers[0]] == ["a"]
        assert sorted(item.task_id for item in layers[1]) == ["b", "c"]
        assert [item.task_id for item in graph.ready(frozenset())] == ["a"]
        assert sorted(item.task_id for item in graph.ready(frozenset({TaskId("a")}))) == [
            "b",
            "c",
        ]


class TestAccords:
    def test_an_unsigned_accord_is_unrepresentable(self) -> None:
        with pytest.raises(ValidationError, match="not fully signed"):
            Accord(
                accord_id=AccordId("acc"),
                conflict_id=ConflictId("cfl"),
                resolution=Defer(yielding=AgentId("a"), until=WorkstreamId("ws-b")),
                signed_by=frozenset({AgentId("a")}),
                participants=frozenset({AgentId("a"), AgentId("b")}),
                agreed_at=NOW,
            )

    def test_a_fully_signed_accord_is_fine(self) -> None:
        accord = Accord(
            accord_id=AccordId("acc"),
            conflict_id=ConflictId("cfl"),
            resolution=Adopt(canonical=AgentId("a"), superseded=AgentId("b")),
            signed_by=frozenset({AgentId("a"), AgentId("b")}),
            participants=frozenset({AgentId("a"), AgentId("b")}),
            agreed_at=NOW,
        )
        assert accord.resolution.kind == "adopt"

    def test_split_assignments_must_be_disjoint(self) -> None:
        with pytest.raises(ValidationError, match="disjoint"):
            Split(
                assignments=(
                    (AgentId("a"), PathScope(patterns=("src/**",))),
                    (AgentId("b"), PathScope(patterns=("src/core/x.py",))),
                )
            )

    def test_a_real_split_is_accepted(self) -> None:
        split = Split(
            assignments=(
                (AgentId("a"), PathScope(patterns=("src/api/**",))),
                (AgentId("b"), PathScope(patterns=("src/store/**",))),
            )
        )
        assert len(split.assignments) == 2

    def test_adopt_needs_two_agents(self) -> None:
        with pytest.raises(ValidationError, match="distinct"):
            Adopt(canonical=AgentId("a"), superseded=AgentId("a"))


class TestContracts:
    def test_breach_detects_a_changed_signature(self) -> None:
        contract = Contract.freeze(
            contract_id=ContractId("c1"),
            provider=TaskId("t1"),
            consumers=frozenset({TaskId("t2")}),
            surface=(symbol("alpha", "def alpha() -> int"),),
        )
        unchanged = (symbol("alpha", "def alpha() -> int"),)
        changed = (symbol("alpha", "def alpha(verbose: bool) -> int"),)
        assert not contract.breached_by(unchanged)
        assert contract.breached_by(changed)

    def test_untouched_surface_is_not_a_breach(self) -> None:
        contract = Contract.freeze(
            contract_id=ContractId("c1"),
            provider=TaskId("t1"),
            consumers=frozenset(),
            surface=(symbol("alpha"),),
        )
        assert not contract.breached_by((symbol("beta", "def beta() -> str"),))

    def test_digest_is_order_independent(self) -> None:
        left = (symbol("alpha"), symbol("beta"))
        assert signature_digest(left) == signature_digest(tuple(reversed(left)))

    def test_provider_cannot_consume_its_own_contract(self) -> None:
        with pytest.raises(ValidationError, match="cannot consume"):
            Contract(
                contract_id=ContractId("c1"),
                provider=TaskId("t1"),
                consumers=frozenset({TaskId("t1")}),
                surface=(symbol("alpha"),),
                signature_digest="x",
            )


def test_gate_report_reports_the_first_failure() -> None:
    report = GateReport(
        checks=(
            CheckResult(name="ruff", command=("ruff",), outcome=CheckOutcome.PASSED),
            CheckResult(
                name="pytest",
                command=("pytest",),
                outcome=CheckOutcome.FAILED,
                exit_code=1,
                log_excerpt="assert 1 == 2",
            ),
        ),
        duration=timedelta(seconds=3),
    )
    assert not report.passed
    assert report.first_failure is not None
    assert "pytest" in report.render()
