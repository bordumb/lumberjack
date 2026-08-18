"""The coordination toolset -- the agent-facing API of the whole harness.

Every awareness mechanism in the spec is reachable from here, and the same toolset is
served over MCP so an agent this project did not write (a Claude Code session in a
worktree, say) joins the swarm under exactly the same rules.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.toolsets import FunctionToolset

from lumberjack.agents.deps import WorkerDeps
from lumberjack.domain.claim import (
    AccessMode,
    Claim,
    LeaseDenied,
    LeaseGranted,
    LeaseQueued,
    PathScope,
    SymbolScope,
)
from lumberjack.domain.contract import AmendmentProposal
from lumberjack.domain.symbols import SymbolRef
from lumberjack.ids import (
    AgentId,
    ContractId,
    LeaseId,
    RepoPath,
    glob_pattern,
    new_proposal_id,
    new_task_id,
    repo_path,
)

__all__ = [
    "AmendmentResult",
    "BlastRadius",
    "ClaimResult",
    "MergeCheck",
    "PeerReport",
    "coordination_toolset",
]


class ClaimResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: Literal["granted", "queued", "denied"]
    lease_id: LeaseId | None = None
    message: str
    peers_also_here: tuple[str, ...] = ()


class PeerReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    peers: tuple[str, ...] = ()
    note: str = ""


class BlastRadius(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dependents: tuple[str, ...] = ()
    affected_paths: tuple[RepoPath, ...] = ()
    warning: str = ""


class MergeCheck(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    clean: bool
    against: str
    conflicted: tuple[RepoPath, ...] = ()
    detail: str = ""


class AmendmentResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    proposal_id: str
    notified: tuple[str, ...] = ()
    message: str


coordination_toolset = FunctionToolset[WorkerDeps]()


@coordination_toolset.tool
async def claim(
    ctx: RunContext[WorkerDeps],
    patterns: list[str],
    mode: AccessMode,
    rationale: str,
) -> ClaimResult:
    """Declare which files you are about to touch, before you touch them.

    Use ``edit`` for ordinary changes -- other agents may hold ``edit`` on the same
    file and that is fine, you will simply both be told.  Use ``exclusive`` for
    renames, deletions, moves, mass reformatting or code generation, which defeat
    line-based merging and therefore have to take turns.  Use ``read`` to be notified
    when someone changes a file you depend on.
    """
    if not patterns:
        raise ModelRetry("claim at least one path or glob")
    decision = await ctx.deps.services.broker.request(
        Claim(
            claimant=ctx.deps.identity,
            workstream=ctx.deps.workstream,
            task=ctx.deps.task_id,
            scope=PathScope(patterns=tuple(glob_pattern(item) for item in patterns)),
            mode=mode,
            rationale=rationale,
        )
    )
    return _render_decision(decision)


@coordination_toolset.tool
async def claim_symbols(
    ctx: RunContext[WorkerDeps],
    symbols: list[SymbolRef],
    mode: AccessMode,
    rationale: str,
) -> ClaimResult:
    """Claim specific definitions rather than whole files.

    Prefer this when two agents must work in one module: symbol-level claims let the
    harness tell you apart, where a file-level claim would make you queue needlessly.
    """
    if not symbols:
        raise ModelRetry("claim at least one symbol")
    decision = await ctx.deps.services.broker.request(
        Claim(
            claimant=ctx.deps.identity,
            workstream=ctx.deps.workstream,
            task=ctx.deps.task_id,
            scope=SymbolScope(symbols=tuple(symbols)),
            mode=mode,
            rationale=rationale,
        )
    )
    return _render_decision(decision)


def _render_decision(decision: LeaseGranted | LeaseQueued | LeaseDenied) -> ClaimResult:
    match decision:
        case LeaseGranted(lease=lease, coexisting=coexisting):
            peers = tuple(str(agent) for agent in coexisting)
            note = (
                "granted. Also editing here: " + ", ".join(peers) + ". "
                "Coordinate before restructuring anything they touch."
                if peers
                else "granted; nobody else holds an overlapping lease."
            )
            return ClaimResult(
                outcome="granted",
                lease_id=lease.lease_id,
                message=note,
                peers_also_here=peers,
            )
        case LeaseQueued(position=position, blockers=blockers):
            return ClaimResult(
                outcome="queued",
                message=(
                    f"queued at position {position} behind "
                    f"{', '.join(blockers)}. Work on something else, or message them."
                ),
            )
        case LeaseDenied(reason=reason, suggestion=suggestion):
            return ClaimResult(
                outcome="denied",
                message=f"denied ({reason.value}). {suggestion or ''}".strip(),
            )


@coordination_toolset.tool
async def release(ctx: RunContext[WorkerDeps], lease_id: LeaseId) -> str:
    """Release a lease once you are done with that scope, so queued peers can proceed."""
    await ctx.deps.services.broker.release(lease_id)
    return f"released {lease_id}"


@coordination_toolset.tool
async def who_touches(ctx: RunContext[WorkerDeps], paths: list[str]) -> PeerReport:
    """Ask whether any other agent is working in these files, right now."""
    services = ctx.deps.services
    now = services.clock.now()
    wanted = set(paths)
    found: list[str] = []
    for other in services.projections.active_workstreams():
        if other.workstream_id == ctx.deps.workstream:
            continue
        observed = set(services.projections.observed_paths(other.workstream_id))
        declared = [
            lease
            for lease in services.projections.leases_of(other.workstream_id, now)
            if any(lease.scope.matches_path(repo_path(path)) for path in wanted)
        ]
        hits = observed & wanted
        if hits or declared:
            modes = ", ".join(sorted({lease.mode.value for lease in declared})) or "observed"
            where = ", ".join(sorted(hits)) or ", ".join(
                lease.scope.describe() for lease in declared
            )
            found.append(f"{other.agent} ({modes}) in {where}")
    return PeerReport(
        peers=tuple(found),
        note="nobody else is in these files" if not found else "coordinate before restructuring",
    )


@coordination_toolset.tool
async def blast_radius(
    ctx: RunContext[WorkerDeps], symbol: SymbolRef, depth: int = 2
) -> BlastRadius:
    """Who breaks if you change the shape of this definition.

    Run this before changing any signature.  A green worktree that breaks a caller in
    another worktree is the single most expensive failure mode in a swarm.
    """
    services = ctx.deps.services
    dependents = await services.indexer.dependents_of(
        symbol, services.projections.repo_map, depth=max(1, min(depth, 4))
    )
    paths = tuple(sorted({item.path for item in dependents}))
    frozen = [
        contract
        for contract in services.projections.contracts.values()
        if contract.covers(symbol) and contract.frozen
    ]
    warning = (
        f"{symbol} is on the frozen surface of {frozen[0].contract_id}; "
        "call propose_amendment before changing its signature"
        if frozen
        else ""
    )
    return BlastRadius(
        dependents=tuple(str(item) for item in sorted(dependents, key=str))[:40],
        affected_paths=paths[:40],
        warning=warning,
    )


@coordination_toolset.tool
async def post_note(
    ctx: RunContext[WorkerDeps],
    topic: str,
    body: str,
    patterns: list[str] | None = None,
) -> str:
    """Leave a note on the shared blackboard.

    Notes reach peers whose scope overlaps ``patterns``, so scope them: an unscoped
    note competes for everyone's context budget.  Good topics: ``decisions``,
    ``conventions``, ``gotchas``, ``handoff``.
    """
    scope = PathScope(patterns=tuple(glob_pattern(item) for item in patterns)) if patterns else None
    note_id = await ctx.deps.services.board.post(
        author=ctx.deps.identity, topic=topic, body=body, scope=scope
    )
    return f"posted {note_id} to {topic}"


@coordination_toolset.tool
async def read_board(
    ctx: RunContext[WorkerDeps], topic: str | None = None, limit: int = 10
) -> tuple[str, ...]:
    """Read the blackboard beyond what your digest already showed you."""
    notes = await ctx.deps.services.board.read(topic=topic, limit=max(1, min(limit, 50)))
    return tuple(note.render() for note in notes)


@coordination_toolset.tool
async def message(ctx: RunContext[WorkerDeps], to: str, subject: str, body: str) -> str:
    """Send a direct message to another agent.

    Delivery is at their next turn boundary, not an interrupt -- so ask a question and
    keep working rather than waiting.
    """
    message_id = await ctx.deps.services.bus.send(
        frm=ctx.deps.identity, to=AgentId(to), subject=subject, body=body
    )
    return f"sent {message_id} to {to}"


@coordination_toolset.tool
async def propose_amendment(
    ctx: RunContext[WorkerDeps],
    contract_id: ContractId,
    before: str,
    after: str,
    migration_note: str,
) -> AmendmentResult:
    """Required before changing a frozen interface.

    The proposal reaches every consumer of the contract so they can adapt, rather than
    discovering the break when the branches meet.
    """
    from lumberjack.domain.events import AmendmentProposed

    services = ctx.deps.services
    contract = services.projections.contracts.get(contract_id)
    if contract is None:
        raise ModelRetry(
            f"no contract {contract_id}; known contracts: "
            f"{', '.join(services.projections.contracts) or 'none'}"
        )
    proposal = AmendmentProposal(
        proposal_id=new_proposal_id(),
        contract_id=contract_id,
        proposer=ctx.deps.identity,
        before=before,
        after=after,
        migration_note=migration_note,
        proposed_at=services.clock.now(),
    )
    await services.ledger.append(AmendmentProposed(proposal=proposal), actor=ctx.deps.identity)
    notified: list[str] = []
    for consumer in contract.consumers:
        found = next(
            (item for item in services.projections.active_workstreams() if item.task == consumer),
            None,
        )
        if found is None:
            continue
        await services.bus.send(
            frm=ctx.deps.identity,
            to=found.agent,
            subject=f"amendment proposed to {contract_id}",
            body=f"{before}\n  ->\n{after}\n\nmigration: {migration_note}",
        )
        notified.append(str(found.agent))
    return AmendmentResult(
        proposal_id=proposal.proposal_id,
        notified=tuple(notified),
        message="consumers notified; proceed once they acknowledge",
    )


@coordination_toolset.tool
async def check_merge(ctx: RunContext[WorkerDeps], against: str = "integration") -> MergeCheck:
    """Ask git whether your work merges cleanly, right now.

    ``against`` is ``integration`` or another agent's id.  This is a real merge, not a
    guess, and it costs about as much as a status call.
    """
    services = ctx.deps.services
    if against == "integration":
        clean, conflicted = await services.oracle.would_land_cleanly(ctx.deps.workstream)
        if not conflicted:
            report = await services.oracle.probe_integration(ctx.deps.workstream)
            if report is not None:
                clean, conflicted = False, tuple(sorted(report.paths))
        return MergeCheck(
            clean=clean,
            against="integration",
            conflicted=conflicted,
            detail="rebase onto integration before landing" if not clean else "clean",
        )

    peer = services.projections.workstream_of(AgentId(against))
    if peer is None:
        raise ModelRetry(f"no active agent {against!r}; use 'integration' or a peer's id")
    report = await services.oracle.probe_pair(ctx.deps.workstream, peer)
    if report is None:
        return MergeCheck(clean=True, against=against, detail="merges cleanly")
    return MergeCheck(
        clean=False,
        against=against,
        conflicted=tuple(sorted(report.paths)),
        detail=report.evidence.splitlines()[0][:200] if report.evidence else "",
    )


@coordination_toolset.tool
async def request_land(ctx: RunContext[WorkerDeps]) -> str:
    """Commit your work and enter the merge train.  Call this when the task is done."""
    services = ctx.deps.services
    tip = await services.git.commit_all(
        ctx.deps.worktree, f"{ctx.deps.task.title} ({ctx.deps.task_id})"
    )
    if tip is None:
        tip = await services.git.head(ctx.deps.worktree)
    position = await services.train.request(ctx.deps.workstream, tip)
    ahead = ", ".join(position.ahead) or "nobody"
    return f"queued to land at position {position.position} (ahead of you: {ahead})"


@coordination_toolset.tool
async def split_task(ctx: RunContext[WorkerDeps], titles_and_intents: list[tuple[str, str]]) -> str:
    """Propose breaking this task up when it turns out to be bigger than it looked."""
    if len(titles_and_intents) < 2:
        raise ModelRetry("a split needs at least two tasks")
    from lumberjack.domain.events import TaskPlanned
    from lumberjack.domain.task import TaskSpec

    created: list[str] = []
    for title, intent in titles_and_intents:
        spec = TaskSpec(task_id=new_task_id(), title=title, intent=intent)
        await ctx.deps.services.ledger.append(TaskPlanned(spec=spec), actor=ctx.deps.identity)
        created.append(spec.task_id)
    return "proposed: " + ", ".join(created)


@coordination_toolset.tool
async def awareness(ctx: RunContext[WorkerDeps]) -> str:
    """Re-read your scoped awareness digest: peers, conflicts, messages, drift."""
    return ctx.deps.services.digest.render(ctx.deps.workstream)
