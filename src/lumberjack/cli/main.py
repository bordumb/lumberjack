"""The ``lj`` command line.

``cyclopts`` rather than ``typer`` because the whole project is annotation-driven:
signatures are the source of truth here too, with no decorator arguments repeating
what the types already say.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import cyclopts

from lumberjack.adapters.clock import SystemClock
from lumberjack.adapters.git_cli import GitCli
from lumberjack.adapters.sqlite_ledger import SqliteLedger
from lumberjack.core.projections import Projections
from lumberjack.domain.events import StandHalted
from lumberjack.domain.workstream import ArbitrationMode, StandConfig
from lumberjack.ids import StandId
from lumberjack.stand import Stand

__all__ = ["app", "main"]

app = cyclopts.App(
    name="lj",
    help="Run a swarm of AI agents in parallel git worktrees, with awareness.",
)

CONFIG_NAME = "lumberjack.json"


def _load_config(repo: Path) -> StandConfig:
    path = repo / CONFIG_NAME
    if path.is_file():
        return StandConfig.model_validate_json(path.read_text()).model_copy(update={"repo": repo})
    return StandConfig(repo=repo)


def _state_root(repo: Path) -> Path:
    return _load_config(repo).resolved_state_root()


def _latest_stand(repo: Path) -> StandId | None:
    root = _state_root(repo)
    if not root.is_dir():
        return None
    stands = sorted(
        (item for item in root.iterdir() if (item / "ledger.db").is_file()),
        key=lambda item: (item / "ledger.db").stat().st_mtime,
    )
    return StandId(stands[-1].name) if stands else None


async def _replay(repo: Path, stand: StandId) -> Projections:
    ledger = await SqliteLedger.open(stand, _state_root(repo) / stand / "ledger.db")
    projections = Projections(stand=stand)
    try:
        await projections.hydrate(ledger)
    finally:
        await ledger.close()
    return projections


@app.command
def init(
    repo: Annotated[Path, cyclopts.Parameter(help="Repository root.")] = Path(),
    *,
    max_parallel: int = 6,
    arbitration: ArbitrationMode = ArbitrationMode.HYBRID,
    model: str = "anthropic:claude-opus-5",
) -> None:
    """Write a lumberjack.json and prepare the state directory."""
    config = StandConfig(repo=repo, max_parallel=max_parallel, arbitration=arbitration, model=model)
    target = repo / CONFIG_NAME
    target.write_text(config.model_dump_json(indent=2, exclude={"repo"}))
    config.resolved_state_root().mkdir(parents=True, exist_ok=True)
    config.resolved_worktree_root().mkdir(parents=True, exist_ok=True)
    gitignore = repo / ".gitignore"
    entry = f"{config.state_root}/\n"
    if not gitignore.is_file() or entry not in gitignore.read_text():
        with gitignore.open("a", encoding="utf-8") as handle:
            handle.write(entry)
    print(f"wrote {target}")
    print(f"state  {config.resolved_state_root()}")
    print(f"arbitration: {arbitration.value}, up to {max_parallel} parallel workstreams")


@app.command
def plan(goal: str, *, repo: Path = Path(), dry_run: bool = True) -> None:
    """Scout the repository and print the task graph, without running anything."""

    async def go() -> None:
        config = _load_config(repo)
        async with Stand.open(config) as stand:
            graph = await stand.plan(goal)
            for layer, wave in enumerate(graph.topological_layers(), start=1):
                print(f"wave {layer}:")
                for spec in wave:
                    scope = spec.predicted_scope.describe() if spec.predicted_scope else "?"
                    print(f"  {spec.task_id}  {spec.title}")
                    print(f"      scope: {scope}")
            if not dry_run:
                print("\n(use `lj run` to execute)")

    asyncio.run(go())


@app.command
def run(
    goal: str,
    *,
    repo: Path = Path(),
    n: Annotated[int | None, cyclopts.Parameter(name=["-n", "--parallel"])] = None,
    arbitration: ArbitrationMode | None = None,
) -> None:
    """Plan and execute: N agents, N worktrees, one integration branch."""

    async def go() -> None:
        config = _load_config(repo)
        if n is not None:
            config = config.model_copy(update={"max_parallel": n})
        if arbitration is not None:
            config = config.model_copy(update={"arbitration": arbitration})
        async with Stand.open(config) as stand:
            print(f"stand {stand.stand_id} on {config.repo}")
            outcome = await stand.run(goal)
            print(outcome.summary())
            for item in outcome.workstreams:
                print(f"  {item.agent}  {item.task.kind:<20} {item.task.spec.title}")
            if outcome.preserved_worktrees:
                print("\npreserved worktrees (unlanded work):")
                for path in outcome.preserved_worktrees:
                    print(f"  {path}")

    asyncio.run(go())


@app.command
def status(*, repo: Path = Path(), stand: str | None = None) -> None:
    """Workstreams, leases, conflicts and the train, replayed from the ledger."""

    async def go() -> None:
        target = StandId(stand) if stand else _latest_stand(repo)
        if target is None:
            print("no stands found; run `lj run` first")
            return
        projections = await _replay(repo, target)
        clock = SystemClock()
        print(f"stand {target}: {projections.goal}")
        print(
            f"integration: {projections.integration_branch} @ "
            f"{(projections.integration_head or '?')[:8]}"
        )
        print(f"\nworkstreams ({len(projections.workstreams)}):")
        for item in projections.workstreams.values():
            task = projections.tasks.get(item.task)
            state = task.kind if task is not None else "unknown"
            drift = f" [{item.drift.behind} behind]" if item.drift.behind else ""
            print(f"  {item.agent:<24} {state:<20} {item.worktree.branch}{drift}")
        leases = projections.active_leases(clock.now())
        print(f"\nleases ({len(leases)}):")
        for lease in leases:
            print(f"  {lease.holder:<24} {lease.mode.value:<10} {lease.scope.describe()}")
        print(f"\nopen conflicts ({len(projections.conflicts)}):")
        for report in projections.conflicts.values():
            print(f"  {report.summary()}")
        if projections.train:
            print(f"\ntrain: {' -> '.join(projections.train)}")

    asyncio.run(go())


@app.command
def conflicts(*, repo: Path = Path(), stand: str | None = None, explain: str | None = None) -> None:
    """Open conflicts, with the oracle's evidence."""

    async def go() -> None:
        target = StandId(stand) if stand else _latest_stand(repo)
        if target is None:
            print("no stands found")
            return
        projections = await _replay(repo, target)
        for report in projections.conflicts.values():
            if explain is not None and report.conflict_id != explain:
                continue
            print(report.summary())
            for item in report.files:
                symbols = ", ".join(str(symbol) for symbol in item.symbols[:4])
                print(f"    {item.path}" + (f"  ({symbols})" if symbols else ""))
            if explain is not None and report.evidence:
                print("    evidence:")
                for line in report.evidence.splitlines()[:20]:
                    print(f"      {line}")
        if not projections.conflicts:
            print("no open conflicts")

    asyncio.run(go())


@app.command
def board(*, repo: Path = Path(), stand: str | None = None, topic: str | None = None) -> None:
    """Read the blackboard."""

    async def go() -> None:
        target = StandId(stand) if stand else _latest_stand(repo)
        if target is None:
            print("no stands found")
            return
        projections = await _replay(repo, target)
        for note in projections.notes:
            if topic is None or note.topic == topic:
                print(note.render())

    asyncio.run(go())


@app.command
def promote(*, repo: Path = Path(), stand: str | None = None, into: str | None = None) -> None:
    """Merge the integration branch into the base branch.  The human gate."""

    async def go() -> None:
        target = StandId(stand) if stand else _latest_stand(repo)
        if target is None:
            print("no stands found")
            return
        config = _load_config(repo)
        projections = await _replay(repo, target)
        git = GitCli(repo=repo)
        destination = into or config.base_ref
        outcome = await git.merge(
            projections.integration_branch,
            destination,
            message=f"promote {target}: {projections.goal}",
        )
        print(f"{outcome.status} -> {destination} @ {(outcome.head or '?')[:8]}")
        if outcome.conflicted:
            print("conflicted: " + ", ".join(outcome.conflicted))

    asyncio.run(go())


@app.command
def halt(*, repo: Path = Path(), stand: str | None = None, reason: str = "operator halt") -> None:
    """Ask a running stand to drain and stop.  Worktrees with unlanded work are kept."""

    async def go() -> None:
        target = StandId(stand) if stand else _latest_stand(repo)
        if target is None:
            print("no stands found")
            return
        ledger = await SqliteLedger.open(target, _state_root(repo) / target / "ledger.db")
        try:
            await ledger.append(StandHalted(reason=reason))
        finally:
            await ledger.close()
        print(f"halt requested for {target}")

    asyncio.run(go())


@app.command
def replay(
    stand: str, *, repo: Path = Path(), kinds: str | None = None, as_json: bool = False
) -> None:
    """Print the event log.  Every projection in the system is a fold over this."""

    async def go() -> None:
        target = StandId(stand)
        ledger = await SqliteLedger.open(target, _state_root(repo) / target / "ledger.db")
        try:
            wanted = frozenset(kinds.split(",")) if kinds else None
            for envelope in await ledger.read(kinds=wanted):
                if as_json:
                    print(json.dumps(json.loads(envelope.model_dump_json())))
                else:
                    print(
                        f"{envelope.seq:>5} {envelope.at:%H:%M:%S} "
                        f"{envelope.actor!s:<24} {envelope.kind}"
                    )
        finally:
            await ledger.close()

    asyncio.run(go())


@app.command
def watch(*, repo: Path = Path(), stand: str | None = None) -> None:
    """Live dashboard: workstream lanes, conflicts, leases, the train."""
    from lumberjack.tui.dashboard import run_dashboard

    target = StandId(stand) if stand else _latest_stand(repo)
    if target is None:
        print("no stands found")
        return
    run_dashboard(repo=repo, stand=target, state_root=_state_root(repo))


@app.command
def serve(*, repo: Path = Path(), stand: str | None = None, transport: str = "stdio") -> None:
    """Serve the coordination toolset over MCP so external agents can join a stand."""
    from lumberjack.server.mcp import serve_stand

    target = StandId(stand) if stand else _latest_stand(repo)
    if target is None:
        print("no stands found; start one with `lj run`")
        return
    asyncio.run(serve_stand(repo=repo, stand=target, transport=transport))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
