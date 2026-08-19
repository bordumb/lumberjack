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

from lumberjack.adapters.claude_code import CoordinationUnavailableError
from lumberjack.adapters.clock import SystemClock
from lumberjack.adapters.git_cli import GitCli
from lumberjack.adapters.sqlite_ledger import SqliteLedger
from lumberjack.agents.outputs import Plan
from lumberjack.core.control import StandControl, stand_alive
from lumberjack.core.projections import Projections
from lumberjack.domain.events import StandHalted, StandRenamed
from lumberjack.domain.request import MODELS, RunRequest
from lumberjack.domain.task import TaskSpec
from lumberjack.domain.workstream import ArbitrationMode, StandConfig
from lumberjack.ids import (
    CommentId,
    CommitSha,
    ConflictId,
    StandId,
    TaskId,
    WorkstreamId,
    repo_path,
)
from lumberjack.ports.git import GitError
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


def _plan_from_specs(repo: Path, specs: list[Path]) -> Plan:
    """One task per spec file.

    The intent names the file rather than inlining it: the agent has a worktree and can
    read it, and a spec pasted into a prompt goes stale the moment anyone edits it.
    """
    tasks: list[TaskSpec] = []
    for spec in specs:
        path = spec if spec.is_absolute() else repo / spec
        if not path.is_file():
            msg = f"no such spec: {path}"
            raise FileNotFoundError(msg)
        relative = path.resolve().relative_to(repo.resolve()).as_posix()
        title = path.stem.replace("_", " ")
        tasks.append(
            TaskSpec(
                task_id=TaskId(path.stem),
                title=title,
                intent=(
                    f"Implement the specification in `{relative}`. Read it first -- it is "
                    "in your worktree. Follow its acceptance criteria exactly, and treat "
                    "its 'out of scope' section as binding: other agents are implementing "
                    "the sibling specs at the same time."
                ),
                acceptance=(
                    f"every acceptance criterion in {relative} is met",
                    "uv run ruff check . passes",
                    "uv run ty check passes",
                    "uv run pytest passes",
                ),
            )
        )
    return Plan(tasks=tuple(tasks), max_parallel=len(tasks))


@app.command
def run(
    goal: str | None = None,
    *,
    repo: Path = Path(),
    spec: Annotated[list[Path] | None, cyclopts.Parameter(name=["--spec"])] = None,
    n: Annotated[int | None, cyclopts.Parameter(name=["-n", "--parallel"])] = None,
    arbitration: ArbitrationMode | None = None,
    runtime: Annotated[
        str | None, cyclopts.Parameter(name=["--runtime"], help="pydantic_ai | claude_code")
    ] = None,
    resume: Annotated[
        str | None,
        cyclopts.Parameter(
            name=["--resume"], help="Continue each task from an earlier stand's branch."
        ),
    ] = None,
    request: Annotated[
        Path | None,
        cyclopts.Parameter(
            name=["--request"],
            help="A RunRequest JSON document describing the agents and their models.",
        ),
    ] = None,
) -> None:
    """Plan and execute: N agents, N worktrees, one integration branch.

    Give a goal to have the foreman decompose it, or one ``--spec`` per file to skip
    planning entirely and put one agent on each specification.
    """

    async def go() -> None:
        config = _load_config(repo)
        if n is not None:
            config = config.model_copy(update={"max_parallel": n})
        if arbitration is not None:
            config = config.model_copy(update={"arbitration": arbitration})
        if runtime is not None:
            config = config.model_copy(update={"worker_runtime": runtime})

        run_request = RunRequest.model_validate_json(request.read_text()) if request else None
        plan = (
            Plan(tasks=run_request.task_specs(), max_parallel=len(run_request.agents))
            if run_request
            else (_plan_from_specs(repo, spec) if spec else None)
        )
        if run_request is not None:
            config = config.model_copy(update={"worker_runtime": run_request.runtime})

        # Continuing a run supplies its own plan, so this has to happen before the
        # check below rejects a command that has neither a goal nor a spec.
        carried_from: StandId | None = None
        carried_goal: str | None = None
        if resume is not None and plan is None:
            carried_from = StandId(resume)
            carried = await _tasks_of_stand(repo, carried_from)
            if not carried:
                print(f"stand {resume} recorded no tasks, so there is nothing to continue")
                raise SystemExit(3)
            plan = Plan(tasks=carried, max_parallel=len(carried))
            carried_goal = f"continue {resume}"

        if plan is not None and n is None:
            config = config.model_copy(update={"max_parallel": len(plan.tasks)})
        goal_text = goal or carried_goal or (run_request.name if run_request else None)
        if goal_text is None and plan is None:
            print("give a goal, or one --spec per specification file")
            return
        description = goal_text or "implement " + ", ".join(
            item.title for item in (plan.tasks if plan else ())
        )

        async with Stand.open(config) as stand:
            if resume is not None:
                assert plan is not None
                stand.supervisor.resumed_from = carried_from or StandId(resume)
                found = await _resume_bases(config.repo, resume, plan)
                if not found:
                    print(f"no branches found for stand {resume}; nothing to resume from")
                    raise SystemExit(3)
                stand.supervisor.resume_bases.update(found)
                for task_id, commit in found.items():
                    print(f"resuming {task_id} from {commit[:8]}")
            if run_request is not None:
                stand.supervisor.model_overrides.update(run_request.models())
            print(f"stand {stand.stand_id} on {config.repo}")
            print(f"runtime: {config.worker_runtime}, up to {config.max_parallel} parallel")
            try:
                outcome = await stand.supervisor.run(description, plan=plan)
            except CoordinationUnavailableError as error:
                print(f"\ncoordination is unavailable, so the stand did not start:\n  {error}")
                print(
                    "\nAgents would have run without claims, awareness or conflict "
                    "checks -- writing code blind while this reported progress."
                )
                raise SystemExit(3) from error
            print(outcome.summary())
            for item in outcome.workstreams:
                print(f"  {item.agent}  {item.task.kind:<20} {item.task.spec.title}")
            if outcome.preserved_worktrees:
                print("\npreserved worktrees (unlanded work):")
                for path in outcome.preserved_worktrees:
                    print(f"  {path}")

    asyncio.run(go())


async def _tasks_of_stand(repo: Path, stand: StandId) -> tuple[TaskSpec, ...]:
    """The tasks a stand was given, read back from its own ledger."""
    projections = await _replay(repo, stand)
    return tuple(projections.specs.values())


async def _resume_bases(repo: Path, stand: str, plan: Plan) -> dict[TaskId, CommitSha]:
    """Map each planned task to the tip of its branch in an earlier stand."""
    git = GitCli(repo=repo)
    found: dict[TaskId, CommitSha] = {}
    for spec in plan.tasks:
        branch = f"lj/{stand}/{spec.task_id}"
        try:
            found[spec.task_id] = await git.resolve(branch)
        except GitError:
            continue
    return found


async def _open_stand(repo: Path, stand: StandId):  # noqa: ANN202 - internal helper
    """Open a stand's ledger for writing, with projections hydrated."""
    from lumberjack.adapters.ast_indexer import AstIndexer
    from lumberjack.adapters.otel import build_telemetry
    from lumberjack.adapters.projecting import ProjectingLedger
    from lumberjack.adapters.traced import instrumented
    from lumberjack.adapters.uv_gate import NullGate
    from lumberjack.core.services import Services

    config = _load_config(repo)
    projections = Projections(stand=stand)
    inner = await SqliteLedger.open(stand, _state_root(repo) / stand / "ledger.db")
    ledger = ProjectingLedger(inner=inner, projections=projections)
    await projections.hydrate(ledger)
    telemetry = build_telemetry(config.telemetry)
    git, gate = instrumented(git=GitCli(repo=repo), gate=NullGate(), telemetry=telemetry)
    services = Services.wire(
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
    return services, inner


@app.command
def comment(
    body: str,
    *,
    file: str,
    line: int,
    repo: Path = Path(),
    stand: str | None = None,
    line_end: int | None = None,
    workstream: str | None = None,
    conflict: str | None = None,
    side: str = "additions",
) -> None:
    """Leave a review comment on a line, addressed into the swarm.

    On a workstream it reaches that agent. On a conflict it reaches both participants,
    because a conflict is a dispute and both sides have to hear it. Either way the
    comment blocks that work from landing until it is resolved.
    """

    async def go() -> None:
        target = StandId(stand) if stand else _latest_stand(repo)
        if target is None:
            print("no stands found")
            raise SystemExit(3)
        services, inner = await _open_stand(repo, target)
        try:
            posted = await services.review.comment(
                body=body,
                file=repo_path(file),
                line_start=line,
                line_end=line_end,
                side=side,
                workstream=WorkstreamId(workstream) if workstream else None,
                conflict_id=ConflictId(conflict) if conflict else None,
            )
            recipients = services.review._recipients(posted)
            print(f"{posted.comment_id} on {posted.file}:{posted.lines}")
            print("delivered to: " + (", ".join(recipients) or "nobody active"))
        finally:
            await inner.close()

    asyncio.run(go())


@app.command
def resolve(comment_id: str, *, repo: Path = Path(), stand: str | None = None) -> None:
    """Mark a review comment resolved, unblocking the workstream it was holding."""

    async def go() -> None:
        target = StandId(stand) if stand else _latest_stand(repo)
        if target is None:
            print("no stands found")
            raise SystemExit(3)
        services, inner = await _open_stand(repo, target)
        try:
            await services.review.resolve(CommentId(comment_id))
            print(f"resolved {comment_id}")
        finally:
            await inner.close()

    asyncio.run(go())


@app.command
def comments(
    *, repo: Path = Path(), stand: str | None = None, include_resolved: bool = False
) -> None:
    """List review comments and whether they are still holding work back."""

    async def go() -> None:
        target = StandId(stand) if stand else _latest_stand(repo)
        if target is None:
            print("no stands found")
            raise SystemExit(3)
        projections = await _replay(repo, target)
        found = [item for item in projections.comments.values() if all or not item.resolved]
        if not found:
            print("no open review comments")
            return
        for item in found:
            state = "resolved" if item.resolved else "open"
            print(f"  {item.comment_id}  [{state}]  {item.render()}")

    asyncio.run(go())


@app.command
def models(*, as_json: bool = False) -> None:
    """List the providers and models a run can be configured with."""
    if as_json:
        print(json.dumps([item.model_dump(mode="json") for item in MODELS]))
        return
    for item in MODELS:
        mark = " (default)" if item.default else ""
        print(f"  {item.provider.value}:{item.id:<28} {item.label}{mark}")
        if item.note:
            print(f"      {item.note}")


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
        ledger_file = _state_root(repo) / target / "ledger.db"
        lifecycle = projections.lifecycle(stand_alive(projections.pid, ledger_file))
        print(f"stand {target} [{lifecycle}]: {projections.title}")
        if lifecycle == "stale":
            print("  the supervisor that started this run is gone; nothing below is running")
        elif lifecycle != "live":
            print(f"  this stand is {lifecycle}; nothing below is running")
        print(
            f"integration: {projections.integration_branch} @ "
            f"{(projections.integration_head or '?')[:8]}"
        )
        print(f"\nworkstreams ({len(projections.workstreams)}):")
        for item in projections.workstreams.values():
            task = projections.tasks.get(item.task)
            state = task.kind if task is not None else "unknown"
            gone = "" if item.worktree.path.is_dir() else "  (worktree removed)"
            print(f"  {item.agent:<24} {state:<20} {item.worktree.branch}{gone}")
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
def resume(*, repo: Path = Path(), stand: str | None = None) -> None:
    """Continue a paused run.

    The same stand, the same task ids and the same branches -- a run is a body of work
    rather than one process lifetime, so this adds a session instead of forking a run.
    """

    async def go() -> None:
        target = StandId(stand) if stand else _latest_stand(repo)
        if target is None:
            print("no stands found")
            raise SystemExit(3)
        opened = await Stand.attach(target, _load_config(repo))
        try:
            outstanding = opened.services.projections.outstanding()
            if not outstanding:
                print(f"{target} has nothing outstanding; every task landed or was abandoned")
                raise SystemExit(0)
            print(f"resuming {target}: {', '.join(outstanding)}")
            print(f"runtime: {opened.config.worker_runtime}")
            outcome = await opened.resume()
            print(outcome.summary())
        except CoordinationUnavailableError as error:
            print(f"\ncoordination is unavailable, so the stand did not resume:\n  {error}")
            raise SystemExit(3) from error
        finally:
            await opened.close()

    asyncio.run(go())


@app.command
def rename(name: str, *, repo: Path = Path(), stand: str | None = None) -> None:
    """Give a run a name.

    The log is append-only, so this adds a label rather than rewriting the goal the
    run was originally given -- both stay visible.
    """

    async def go() -> None:
        target = StandId(stand) if stand else _latest_stand(repo)
        if target is None:
            print("no stands found")
            raise SystemExit(3)
        ledger = await SqliteLedger.open(target, _state_root(repo) / target / "ledger.db")
        try:
            await ledger.append(StandRenamed(name=name))
        finally:
            await ledger.close()
        print(f"{target} is now {name!r}")

    asyncio.run(go())


@app.command
def delete(
    *,
    repo: Path = Path(),
    stand: str | None = None,
    force: bool = False,
    drop_branches: bool = False,
) -> None:
    """Delete a run: its ledger, its worktrees, and optionally its branches.

    Branches are kept by default. A branch can hold work that never landed, and a
    deletion that silently discards it is not one anybody can undo.
    """

    async def go() -> None:
        target = StandId(stand) if stand else _latest_stand(repo)
        if target is None:
            print("no stands found")
            raise SystemExit(3)
        config = _load_config(repo)
        projections = await _replay(repo, target)
        control = StandControl(repo=repo, config=config)
        git = GitCli(repo=repo)

        plan = await control.plan_deletion(target, projections, git)
        print(f"{target}: {plan.describe()}")
        if plan.running and not force:
            print("  it is still running. Halt it first, or pass --force.")
            raise SystemExit(1)
        if plan.unlanded and not force:
            for branch in plan.unlanded:
                print(f"  holds unlanded work: {branch}")
            print("  pass --force to delete anyway; branches are kept unless --drop-branches.")
            raise SystemExit(1)

        await control.delete(target, projections, git, drop_branches=drop_branches)
        kept = "" if drop_branches else f" ({len(plan.branches)} branch(es) kept)"
        print(f"deleted {target}{kept}")

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
