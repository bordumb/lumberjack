"""Serve the coordination toolset over MCP.

This is what makes Lumberjack a harness rather than a framework: an agent it did not
write -- a Claude Code session started inside a worktree, say -- attaches here and
becomes a first-class workstream, claiming, negotiating and landing under exactly the
same rules as the built-in workers.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from lumberjack.adapters.ast_indexer import AstIndexer
from lumberjack.adapters.clock import SystemClock
from lumberjack.adapters.git_cli import GitCli
from lumberjack.adapters.otel import build_telemetry
from lumberjack.adapters.projecting import ProjectingLedger
from lumberjack.adapters.sqlite_ledger import SqliteLedger
from lumberjack.adapters.traced import instrumented
from lumberjack.adapters.uv_gate import CommandGate
from lumberjack.core.projections import Projections
from lumberjack.core.services import Services
from lumberjack.domain.claim import AccessMode, Claim, PathScope
from lumberjack.domain.workstream import StandConfig
from lumberjack.ids import (
    AgentId,
    LeaseId,
    StandId,
    WorkstreamId,
    glob_pattern,
    repo_path,
)

__all__ = ["build_server", "serve_stand"]


@dataclass(slots=True)
class _Session:
    """Which workstream the connected agent is acting as."""

    services: Services
    agent: AgentId | None = None
    workstream: WorkstreamId | None = None

    def bind(self, agent: str) -> tuple[AgentId, WorkstreamId]:
        identity = AgentId(agent)
        workstream = self.services.projections.workstream_of(identity)
        if workstream is None:
            known = ", ".join(
                str(item.agent) for item in self.services.projections.active_workstreams()
            )
            msg = f"{agent} is not an active agent in this stand. Active: {known or 'none'}"
            raise ValueError(msg)
        self.agent, self.workstream = identity, workstream
        return identity, workstream


async def _open_services(repo: Path, stand: StandId) -> Services:
    config_path = repo / "lumberjack.json"
    config = (
        StandConfig.model_validate_json(config_path.read_text()).model_copy(update={"repo": repo})
        if config_path.is_file()
        else StandConfig(repo=repo)
    )
    projections = Projections(stand=stand)
    inner = await SqliteLedger.open(stand, config.resolved_state_root() / stand / "ledger.db")
    ledger = ProjectingLedger(inner=inner, projections=projections)
    await projections.hydrate(ledger)
    telemetry = build_telemetry(config.telemetry)
    git, gate = instrumented(
        git=GitCli(repo=repo),
        gate=CommandGate(commands=config.gate_commands),
        telemetry=telemetry,
    )
    return Services.wire(
        stand=stand,
        config=config,
        clock=SystemClock(),
        git=git,
        ledger=ledger,
        indexer=AstIndexer(),
        gate=gate,
        projections=projections,
        telemetry=telemetry,
    )


def build_server(services: Services) -> FastMCP:
    """Expose the coordination surface.  Deliberately no file editing: the attached
    agent already has its own tools for that inside its worktree."""
    session = _Session(services=services)
    server = FastMCP("lumberjack")

    async def _refresh() -> None:
        await services.projections.hydrate(services.ledger)

    @server.tool()
    async def join(agent: str) -> str:
        """Bind this connection to an agent identity in the stand."""
        await _refresh()
        identity, workstream = session.bind(agent)
        found = services.projections.workstreams[workstream]
        return (
            f"joined as {identity} on {workstream}; worktree {found.worktree.path} "
            f"(branch {found.worktree.branch})"
        )

    def _require() -> tuple[AgentId, WorkstreamId]:
        if session.agent is None or session.workstream is None:
            msg = "call join(agent=...) first"
            raise ValueError(msg)
        return session.agent, session.workstream

    @server.tool()
    async def claim(patterns: list[str], mode: str, rationale: str) -> dict[str, Any]:
        """Declare the files you are about to touch. mode: read | edit | exclusive."""
        await _refresh()
        agent, workstream = _require()
        found = services.projections.workstreams[workstream]
        decision = await services.broker.request(
            Claim(
                claimant=agent,
                workstream=workstream,
                task=found.task,
                scope=PathScope(patterns=tuple(glob_pattern(item) for item in patterns)),
                mode=AccessMode(mode),
                rationale=rationale,
            )
        )
        return decision.model_dump(mode="json")

    @server.tool()
    async def release(lease_id: str) -> str:
        """Release a lease so queued peers may proceed."""
        await _refresh()
        await services.broker.release(LeaseId(lease_id))
        return f"released {lease_id}"

    @server.tool()
    async def awareness() -> str:
        """Your scoped digest: peers in your files, conflicts, messages, drift."""
        await _refresh()
        _, workstream = _require()
        return services.digest.render(workstream)

    @server.tool()
    async def who_touches(paths: list[str]) -> list[str]:
        """Which other agents are working in these files right now."""
        await _refresh()
        _, workstream = _require()
        now = services.clock.now()
        wanted = {repo_path(item) for item in paths}
        found: list[str] = []
        for other in services.projections.active_workstreams():
            if other.workstream_id == workstream:
                continue
            observed = set(services.projections.observed_paths(other.workstream_id))
            declared = [
                lease
                for lease in services.projections.leases_of(other.workstream_id, now)
                if any(lease.scope.matches_path(path) for path in wanted)
            ]
            if observed & wanted or declared:
                found.append(f"{other.agent}: {', '.join(sorted(observed & wanted)) or 'declared'}")
        return found

    @server.tool()
    async def check_merge(against: str = "integration") -> dict[str, Any]:
        """Ask git -- with a real merge -- whether your work merges cleanly."""
        await _refresh()
        _, workstream = _require()
        if against == "integration":
            clean, conflicted = await services.oracle.would_land_cleanly(workstream)
            return {"clean": clean, "against": against, "conflicted": list(conflicted)}
        peer = services.projections.workstream_of(AgentId(against))
        if peer is None:
            return {"clean": True, "against": against, "conflicted": [], "note": "unknown agent"}
        report = await services.oracle.probe_pair(workstream, peer)
        return {
            "clean": report is None,
            "against": against,
            "conflicted": sorted(report.paths) if report else [],
        }

    @server.tool()
    async def post_note(topic: str, body: str, patterns: list[str] | None = None) -> str:
        """Leave a scoped note on the shared blackboard."""
        await _refresh()
        agent, _ = _require()
        scope = (
            PathScope(patterns=tuple(glob_pattern(item) for item in patterns)) if patterns else None
        )
        note_id = await services.board.post(author=agent, topic=topic, body=body, scope=scope)
        return f"posted {note_id}"

    @server.tool()
    async def read_board(topic: str | None = None, limit: int = 10) -> list[str]:
        """Read the blackboard."""
        await _refresh()
        notes = await services.board.read(topic=topic, limit=limit)
        return [note.render() for note in notes]

    @server.tool()
    async def message(to: str, subject: str, body: str) -> str:
        """Send a direct message to another agent in the stand."""
        await _refresh()
        agent, _ = _require()
        message_id = await services.bus.send(frm=agent, to=AgentId(to), subject=subject, body=body)
        return f"sent {message_id}"

    @server.tool()
    async def request_land() -> str:
        """Commit your worktree and enter the merge train."""
        await _refresh()
        agent, workstream = _require()
        found = services.projections.workstreams[workstream]
        tip = await services.git.commit_all(found.worktree, f"work by {agent}")
        tip = tip or await services.git.head(found.worktree)
        position = await services.train.request(workstream, tip)
        return f"queued at position {position.position}"

    return server


async def serve_stand(*, repo: Path, stand: StandId, transport: str = "stdio") -> None:
    services = await _open_services(repo, stand)
    server = build_server(services)
    await server.run_stdio_async() if transport == "stdio" else await server.run_sse_async()
