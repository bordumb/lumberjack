"""The observation plane: what the sensor notices that nobody declared."""

from __future__ import annotations

from lumberjack.core.sensor import WorktreeSensor
from lumberjack.core.services import Services
from lumberjack.domain.claim import AccessMode, Claim, PathScope
from lumberjack.domain.conflict import ConflictSource, Severity
from lumberjack.domain.contract import Contract
from lumberjack.domain.events import ContractFrozen
from lumberjack.domain.symbols import SymbolRef
from lumberjack.domain.workstream import Workstream
from lumberjack.ids import ContractId


def sensor_for(services: Services, workstream: Workstream) -> WorktreeSensor:
    return WorktreeSensor(
        workstream=workstream,
        git=services.git,
        ledger=services.ledger,
        projections=services.projections,
        indexer=services.indexer,
        clock=services.clock,
        config=services.config,
        broker=services.broker,
    )


async def test_delta_reports_paths_and_symbols(services: Services, make_workstream) -> None:
    workstream = await make_workstream("a")
    (workstream.worktree.path / "pkg" / "core.py").write_text(
        "def alpha(verbose: bool) -> int:\n    return 1\n"
    )

    delta = await sensor_for(services, workstream).scan()

    assert delta is not None
    assert delta.paths == ("pkg/core.py",)
    assert any(symbol.qualname == "alpha" for symbol in delta.symbols)
    assert delta.dirty


async def test_undeclared_work_raises_a_warning_and_files_a_claim(
    services: Services, make_workstream
) -> None:
    """Agents discover scope as they go, so this warns and adapts rather than halting."""
    workstream = await make_workstream("a")
    (workstream.worktree.path / "pkg" / "core.py").write_text("def alpha() -> int:\n    return 9\n")

    await sensor_for(services, workstream).scan()

    violations = services.projections.violations
    assert any(item.violation.code == "undeclared_scope" for item in violations)
    assert any(
        lease.scope.matches_path("pkg/core.py")
        for lease in services.projections.leases_of(workstream.workstream_id, services.clock.now())
    )


async def test_declared_work_raises_nothing(services: Services, make_workstream) -> None:
    workstream = await make_workstream("a")
    await services.broker.request(
        Claim(
            claimant=workstream.agent,
            workstream=workstream.workstream_id,
            task=workstream.task,
            scope=PathScope(patterns=("pkg/**",)),
            mode=AccessMode.EDIT,
            rationale="declared up front",
        )
    )
    (workstream.worktree.path / "pkg" / "core.py").write_text("def alpha() -> int:\n    return 9\n")

    await sensor_for(services, workstream).scan()

    assert not [
        item
        for item in services.projections.violations
        if item.violation.code == "undeclared_scope"
    ]


async def test_undeclared_rename_is_blocking(services: Services, make_workstream) -> None:
    """Structural change is the one case line-based merging cannot rescue."""
    workstream = await make_workstream("a")
    await services.broker.request(
        Claim(
            claimant=workstream.agent,
            workstream=workstream.workstream_id,
            task=workstream.task,
            scope=PathScope(patterns=("pkg/**",)),
            mode=AccessMode.EDIT,
            rationale="ordinary edits",
        )
    )
    path = workstream.worktree.path / "pkg" / "core.py"
    path.rename(path.with_name("kernel.py"))

    await sensor_for(services, workstream).scan()

    structural = [
        item
        for item in services.projections.violations
        if item.violation.code == "undeclared_structural_change"
    ]
    assert structural
    assert structural[0].severity is Severity.BLOCK


async def test_blast_radius_warns_the_dependent_workstream(
    services: Services, make_workstream
) -> None:
    """Agent A changes a symbol; agent B, who imports it, is told before the merge."""
    base = await services.git.resolve("main")
    from lumberjack.agents.scout import Scout

    services.projections.repo_map = await Scout(git=services.git, indexer=services.indexer).survey(
        base
    )

    provider = await make_workstream("a")
    consumer = await make_workstream("b")
    (consumer.worktree.path / "pkg" / "caller.py").write_text(
        "from pkg.core import alpha\n\n\ndef use() -> int:\n    return alpha() + 1\n"
    )
    await sensor_for(services, consumer).scan()

    (provider.worktree.path / "pkg" / "core.py").write_text(
        "def alpha(verbose: bool) -> int:\n    return 1\n"
    )
    await sensor_for(services, provider).scan()

    blast = [
        report
        for report in services.projections.conflicts.values()
        if report.source is ConflictSource.BLAST_RADIUS
    ]
    assert blast, "the consumer should have been warned"
    assert consumer.workstream_id in blast[0].participants


async def test_contract_breach_blocks_and_notifies(services: Services, make_workstream) -> None:
    provider = await make_workstream("a")
    surface = (
        SymbolRef(
            module="pkg.core",
            qualname="alpha",
            path="pkg/core.py",
            signature="def alpha() -> int",
        ),
    )
    await services.ledger.append(
        ContractFrozen(
            contract=Contract.freeze(
                contract_id=ContractId("c1"),
                provider=provider.task,
                consumers=frozenset(),
                surface=surface,
            )
        )
    )
    (provider.worktree.path / "pkg" / "core.py").write_text(
        "def alpha(verbose: bool) -> int:\n    return 1\n"
    )

    await sensor_for(services, provider).scan()

    breaches = [
        item for item in services.projections.violations if item.violation.code == "contract_breach"
    ]
    assert breaches
    assert breaches[0].severity is Severity.BLOCK


async def test_claim_overlap_is_announced_as_a_notice(services: Services, make_workstream) -> None:
    left = await make_workstream("a")
    right = await make_workstream("b")
    for workstream in (left, right):
        await services.broker.request(
            Claim(
                claimant=workstream.agent,
                workstream=workstream.workstream_id,
                task=workstream.task,
                scope=PathScope(patterns=("pkg/**",)),
                mode=AccessMode.EDIT,
                rationale="both here",
            )
        )

    await sensor_for(services, left).announce_overlaps()

    overlaps = [
        report
        for report in services.projections.conflicts.values()
        if report.source is ConflictSource.CLAIM_OVERLAP
    ]
    assert overlaps
    assert overlaps[0].severity is Severity.NOTICE
