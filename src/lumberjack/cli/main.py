"""The ``lj`` command line.

``cyclopts`` rather than ``typer`` because the whole project is annotation-driven:
signatures are the source of truth here too, with no decorator arguments repeating
what the types already say.

Nothing in this module writes to a stream directly.  Commands gather state, hand it to
:mod:`lumberjack.cli.render`, and print the result through
:class:`lumberjack.cli.output.Output` -- which is what makes ``--json`` safe to pipe and
the whole surface testable.  Every command returns an
:class:`~lumberjack.cli.render.ExitCode`; ``_exit`` turns anything but ``OK`` into a
``SystemExit``, because a harness that always exits zero cannot be used in CI.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Annotated

import cyclopts

from lumberjack.adapters.claude_code import CoordinationUnavailableError
from lumberjack.adapters.clock import SystemClock
from lumberjack.adapters.git_cli import GitCli
from lumberjack.adapters.sqlite_ledger import SqliteLedger
from lumberjack.agents.outputs import Plan
from lumberjack.cli import live, render
from lumberjack.cli.output import Output, default_output
from lumberjack.cli.project import detect_gate
from lumberjack.cli.render import ExitCode
from lumberjack.core.control import StandControl, stand_alive
from lumberjack.core.projections import Projections
from lumberjack.core.services import Services
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

log = logging.getLogger(__name__)

app = cyclopts.App(
    name="lj",
    help="Run a swarm of AI agents in parallel git worktrees, with awareness.",
)

CONFIG_NAME = "lumberjack.json"

AsJson = Annotated[
    bool,
    cyclopts.Parameter(name=["--json", "--as-json"], help="Machine-readable output."),
]
"""``--json`` everywhere it is offered, so scripts never have to parse a table.

``--as-json`` stays as an alias because ``lj models --as-json`` is already wired into
the web interface, and renaming a flag out from under a running caller is not a fix.
"""


def _out() -> Output:
    return default_output()


def _exit(code: ExitCode) -> None:
    """Leave with a code a shell can branch on.  ``OK`` returns rather than raising."""
    if code is not ExitCode.OK:
        raise SystemExit(int(code))


def _run(command: Callable[[], Coroutine[object, object, ExitCode]]) -> None:
    _exit(asyncio.run(command()))


def _load_config(repo: Path) -> StandConfig:
    path = repo / CONFIG_NAME
    if path.is_file():
        return StandConfig.model_validate_json(path.read_text()).model_copy(update={"repo": repo})
    return StandConfig(repo=repo)


def _state_root(repo: Path) -> Path:
    return _load_config(repo).resolved_state_root()


def _ledger_path(repo: Path, stand: StandId) -> Path:
    return _state_root(repo) / stand / "ledger.db"


def _latest_stand(repo: Path) -> StandId | None:
    root = _state_root(repo)
    if not root.is_dir():
        return None
    stands = sorted(
        (item for item in root.iterdir() if (item / "ledger.db").is_file()),
        key=lambda item: (item / "ledger.db").stat().st_mtime,
    )
    return StandId(stands[-1].name) if stands else None


def _require_stand(repo: Path, stand: str | None, *, action: str) -> StandId:
    """The stand to act on, or a message saying what to do instead.

    A named stand with no ledger is the same failure as no stands at all: the command
    cannot proceed, and saying so beats silently creating an empty database.
    """
    target = StandId(stand) if stand else _latest_stand(repo)
    if target is None or not _ledger_path(repo, target).is_file():
        _out().problem(render.no_stands(repo, action=action))
        raise SystemExit(int(ExitCode.NO_STAND))
    return target


async def _replay(repo: Path, stand: StandId) -> Projections:
    ledger = await SqliteLedger.open(stand, _ledger_path(repo, stand))
    projections = Projections(stand=stand)
    try:
        await projections.hydrate(ledger)
    finally:
        await ledger.close()
    return projections


def _usage_by_workstream(services: Services) -> render.UsageByWorkstream | None:
    """Per-workstream token counts, once 0002's usage ledger is wired into ``Services``.

    Read structurally rather than imported: 0002 and 0003 are being built at the same
    time, and a display that hard-imports an interface which does not exist yet blocks
    on a sibling instead of rendering everything else it knows.
    """
    ledger = getattr(services, "usage", None)
    if ledger is None:
        return None
    try:
        return {
            workstream: ledger.for_workstream(workstream)
            for workstream in services.projections.workstreams
        }
    except AttributeError as error:
        # The usage ledger landed with a different shape than 0002 published. Losing a
        # column is a fair trade against failing a run that has otherwise finished.
        log.warning("usage ledger does not answer for_workstream(): %s", error)
        return None


def _usage_total(services: Services) -> render.UsageTotalsLike | None:
    ledger = getattr(services, "usage", None)
    if ledger is None:
        return None
    try:
        return ledger.totals()
    except AttributeError as error:
        log.warning("usage ledger does not answer totals(): %s", error)
        return None


@app.command
def init(
    repo: Annotated[Path, cyclopts.Parameter(help="Repository root.")] = Path(),
    *,
    max_parallel: int = 6,
    arbitration: ArbitrationMode = ArbitrationMode.HYBRID,
    model: str = "anthropic:claude-opus-5",
) -> None:
    """Write a lumberjack.json and prepare the state directory.

    The gate commands come from what is in the repository rather than from a default:
    writing ``uv run pytest`` into a Node project teaches its agents on the first bounce
    that the gate is noise.
    """
    detection = detect_gate(repo)
    config = StandConfig(
        repo=repo,
        max_parallel=max_parallel,
        arbitration=arbitration,
        model=model,
        gate_commands=detection.commands,
    )
    target = repo / CONFIG_NAME
    target.write_text(config.model_dump_json(indent=2, exclude={"repo"}))
    config.resolved_state_root().mkdir(parents=True, exist_ok=True)
    config.resolved_worktree_root().mkdir(parents=True, exist_ok=True)
    gitignore = repo / ".gitignore"
    entry = f"{config.state_root}/\n"
    if not gitignore.is_file() or entry not in gitignore.read_text():
        with gitignore.open("a", encoding="utf-8") as handle:
            handle.write(entry)
    _out().emit(
        render.init_report(
            config_path=target,
            state_root=config.resolved_state_root(),
            detection=detection,
            arbitration=arbitration,
            max_parallel=max_parallel,
        )
    )


@app.command
def plan(goal: str, *, repo: Path = Path()) -> None:
    """Scout the repository and print the task graph, without running anything.

    Nothing here executes: ``lj run`` is the command that does.  (The old ``--dry-run``
    flag defaulted to true and changed nothing but a closing hint, so it is gone.)
    """

    async def go() -> ExitCode:
        config = _load_config(repo)
        async with Stand.open(config) as stand:
            graph = await stand.plan(goal)
            _out().emit(render.plan_view(graph))
        return ExitCode.OK

    _run(go)


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

    The run renders live off the ledger while it happens.  Exit code is ``0`` only when
    every task landed; a blocked task exits ``1`` so CI can tell the difference.
    """

    async def go() -> ExitCode:
        out = _out()
        config = _load_config(repo)
        if n is not None:
            config = config.model_copy(update={"max_parallel": n})
        if arbitration is not None:
            config = config.model_copy(update={"arbitration": arbitration})
        if runtime is not None:
            config = config.model_copy(update={"worker_runtime": runtime})

        try:
            run_request = RunRequest.model_validate_json(request.read_text()) if request else None
            plan = (
                Plan(tasks=run_request.task_specs(), max_parallel=len(run_request.agents))
                if run_request
                else (_plan_from_specs(repo, spec) if spec else None)
            )
        except (OSError, ValueError) as error:
            out.problem(render.usage_error(str(error)))
            return ExitCode.USAGE
        if run_request is not None:
            config = config.model_copy(update={"worker_runtime": run_request.runtime})

        # Continuing a run supplies its own plan, so this has to happen before the
        # check below rejects a command that has neither a goal nor a spec.
        carried_from: StandId | None = None
        carried_goal: str | None = None
        if resume is not None and plan is None:
            carried_from = _require_stand(repo, resume, action="run --resume")
            carried = await _tasks_of_stand(repo, carried_from)
            if not carried:
                out.problem(
                    render.usage_error(
                        f"stand {resume} recorded no tasks, so there is nothing to continue"
                    )
                )
                return ExitCode.NO_STAND
            plan = Plan(tasks=carried, max_parallel=len(carried))
            carried_goal = f"continue {resume}"

        if plan is not None and n is None:
            config = config.model_copy(update={"max_parallel": len(plan.tasks)})
        goal_text = goal or carried_goal or (run_request.name if run_request else None)
        if goal_text is None and plan is None:
            out.problem(render.usage_error("give a goal, or one --spec per specification file"))
            return ExitCode.USAGE
        description = goal_text or "implement " + ", ".join(
            item.title for item in (plan.tasks if plan else ())
        )

        async with Stand.open(config) as stand:
            if resume is not None:
                assert plan is not None
                stand.supervisor.resumed_from = carried_from or StandId(resume)
                found = await _resume_bases(config.repo, resume, plan)
                if not found:
                    out.problem(
                        render.usage_error(f"no branches found for stand {resume} to resume from")
                    )
                    return ExitCode.NO_STAND
                stand.supervisor.resume_bases.update(found)
                out.emit(render.resume_bases(found))
            if run_request is not None:
                stand.supervisor.model_overrides.update(run_request.models())
            out.emit(
                render.run_banner(
                    stand=stand.stand_id,
                    repo=config.repo,
                    runtime=config.worker_runtime,
                    parallel=config.max_parallel,
                )
            )
            watcher = asyncio.create_task(
                live.follow(stand.services.ledger, stand=stand.stand_id, output=out)
            )
            try:
                outcome = await stand.supervisor.run(description, plan=plan)
            except CoordinationUnavailableError as error:
                out.problem(render.coordination_unavailable(error))
                return ExitCode.NO_STAND
            finally:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            out.emit(render.outcome_report(outcome, usage=_usage_total(stand.services)))
            code = render.exit_code_for(outcome)
        _report_preserved(stand, out)
        return code

    _run(go)


def _report_preserved(stand: Stand, out: Output) -> None:
    """Say which worktrees are still on disk, and why, once teardown has decided.

    Only reachable after the stand has closed: until then "kept because its work never
    landed" and "kept because removing it failed" are indistinguishable, and the second
    is the one the operator has to do something about.
    """
    kept = render.preserved_report(stand.preserved_worktrees)
    if kept is not None:
        out.emit(kept)


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


async def _open_stand(repo: Path, stand: StandId) -> tuple[Services, SqliteLedger]:
    """Open a stand's ledger for writing, with projections hydrated."""
    from lumberjack.adapters.ast_indexer import AstIndexer
    from lumberjack.adapters.otel import build_telemetry
    from lumberjack.adapters.projecting import ProjectingLedger
    from lumberjack.adapters.traced import instrumented
    from lumberjack.adapters.uv_gate import NullGate

    config = _load_config(repo)
    projections = Projections(stand=stand)
    inner = await SqliteLedger.open(stand, _ledger_path(repo, stand))
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

    async def go() -> ExitCode:
        target = _require_stand(repo, stand, action="comment")
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
            # `_recipients` is the only form the review service offers; a public one
            # belongs to whoever owns core/review.py, not to the rendering change.
            recipients = services.review._recipients(posted)
            _out().emit(render.comment_receipt(posted, recipients))
        finally:
            await inner.close()
        return ExitCode.OK

    _run(go)


@app.command
def resolve(comment_id: str, *, repo: Path = Path(), stand: str | None = None) -> None:
    """Mark a review comment resolved, unblocking the workstream it was holding."""

    async def go() -> ExitCode:
        target = _require_stand(repo, stand, action="resolve")
        services, inner = await _open_stand(repo, target)
        try:
            await services.review.resolve(CommentId(comment_id))
            _out().line(f"resolved {comment_id}", style="green")
        finally:
            await inner.close()
        return ExitCode.OK

    _run(go)


@app.command
def comments(
    *,
    repo: Path = Path(),
    stand: str | None = None,
    include_resolved: bool = False,
    as_json: AsJson = False,
) -> None:
    """List review comments and whether they are still holding work back."""

    async def go() -> ExitCode:
        target = _require_stand(repo, stand, action="comments")
        projections = await _replay(repo, target)
        found = [
            item
            for item in projections.comments.values()
            if include_resolved or not item.resolved
        ]
        if as_json:
            _out().json(render.comments_json(found))
        else:
            _out().emit(render.comments_view(found))
        return ExitCode.OK

    _run(go)


@app.command
def models(*, as_json: AsJson = False) -> None:
    """List the providers and models a run can be configured with."""
    if as_json:
        _out().json([item.model_dump(mode="json") for item in MODELS])
        return
    _out().emit(render.models_view(MODELS))


@app.command
def status(
    *, repo: Path = Path(), stand: str | None = None, as_json: AsJson = False
) -> None:
    """Workstreams, leases, conflicts and the train, replayed from the ledger."""

    async def go() -> ExitCode:
        target = _require_stand(repo, stand, action="status")
        projections = await _replay(repo, target)
        lifecycle = projections.lifecycle(
            stand_alive(projections.pid, _ledger_path(repo, target))
        )
        present = live.worktrees_present(projections)
        now = SystemClock().now()
        if as_json:
            _out().json(
                render.status_json(
                    projections,
                    stand=target,
                    lifecycle=lifecycle,
                    now=now,
                    present=present,
                )
            )
        else:
            _out().emit(
                render.status_view(
                    projections,
                    stand=target,
                    lifecycle=lifecycle,
                    now=now,
                    present=present,
                )
            )
        return ExitCode.OK

    _run(go)


@app.command
def conflicts(
    *,
    repo: Path = Path(),
    stand: str | None = None,
    explain: str | None = None,
    as_json: AsJson = False,
) -> None:
    """Open conflicts, with the oracle's evidence.

    ``--explain <id>`` adds the conflicting hunks and the negotiation transcript -- the
    single most informative thing the system knows about a pair of agents.
    """

    async def go() -> ExitCode:
        target = _require_stand(repo, stand, action="conflicts")
        projections = await _replay(repo, target)
        if explain is not None and ConflictId(explain) not in projections.conflicts:
            _out().problem(render.usage_error(f"no open conflict {explain} in {target}"))
            return ExitCode.USAGE
        if as_json:
            _out().json(render.conflicts_json(projections))
        else:
            _out().emit(
                render.conflicts_view(
                    projections, explain=ConflictId(explain) if explain else None
                )
            )
        return ExitCode.OK

    _run(go)


@app.command
def board(
    *,
    repo: Path = Path(),
    stand: str | None = None,
    topic: str | None = None,
    as_json: AsJson = False,
) -> None:
    """Read the blackboard."""

    async def go() -> ExitCode:
        target = _require_stand(repo, stand, action="board")
        projections = await _replay(repo, target)
        notes = [note for note in projections.notes if topic is None or note.topic == topic]
        if as_json:
            _out().json(render.board_json(notes))
        else:
            _out().emit(render.board_view(notes))
        return ExitCode.OK

    _run(go)


@app.command
def promote(*, repo: Path = Path(), stand: str | None = None, into: str | None = None) -> None:
    """Merge the integration branch into the base branch.  The human gate."""

    async def go() -> ExitCode:
        target = _require_stand(repo, stand, action="promote")
        config = _load_config(repo)
        projections = await _replay(repo, target)
        git = GitCli(repo=repo)
        destination = into or config.base_ref
        outcome = await git.merge(
            projections.integration_branch,
            destination,
            message=f"promote {target}: {projections.goal}",
        )
        _out().emit(render.promote_report(outcome, destination=destination))
        return ExitCode.PARTIAL if outcome.conflicted else ExitCode.OK

    _run(go)


@app.command
def halt(*, repo: Path = Path(), stand: str | None = None, reason: str = "operator halt") -> None:
    """Ask a running stand to drain and stop.  Worktrees with unlanded work are kept."""

    async def go() -> ExitCode:
        target = _require_stand(repo, stand, action="halt")
        ledger = await SqliteLedger.open(target, _ledger_path(repo, target))
        try:
            await ledger.append(StandHalted(reason=reason))
        finally:
            await ledger.close()
        _out().line(f"halt requested for {target}", style="yellow")
        return ExitCode.OK

    _run(go)


@app.command
def resume(*, repo: Path = Path(), stand: str | None = None) -> None:
    """Continue a paused run.

    The same stand, the same task ids and the same branches -- a run is a body of work
    rather than one process lifetime, so this adds a session instead of forking a run.
    """

    async def go() -> ExitCode:
        out = _out()
        target = _require_stand(repo, stand, action="resume")
        opened = await Stand.attach(target, _load_config(repo))
        try:
            outstanding = opened.services.projections.outstanding()
            if not outstanding:
                out.line(f"{target} has nothing outstanding; every task landed or was abandoned")
                return ExitCode.OK
            out.line(f"resuming {target}: {', '.join(outstanding)}", style="bold")
            out.line(f"runtime: {opened.config.worker_runtime}", style="dim")
            watcher = asyncio.create_task(
                live.follow(opened.services.ledger, stand=target, output=out)
            )
            try:
                outcome = await opened.resume()
            finally:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            out.emit(render.outcome_report(outcome, usage=_usage_total(opened.services)))
            return render.exit_code_for(outcome)
        except CoordinationUnavailableError as error:
            out.problem(render.coordination_unavailable(error, verb="resume"))
            return ExitCode.NO_STAND
        finally:
            await opened.close()
            _report_preserved(opened, out)

    _run(go)


@app.command
def rename(name: str, *, repo: Path = Path(), stand: str | None = None) -> None:
    """Give a run a name.

    The log is append-only, so this adds a label rather than rewriting the goal the
    run was originally given -- both stay visible.
    """

    async def go() -> ExitCode:
        target = _require_stand(repo, stand, action="rename")
        ledger = await SqliteLedger.open(target, _ledger_path(repo, target))
        try:
            await ledger.append(StandRenamed(name=name))
        finally:
            await ledger.close()
        _out().line(f"{target} is now {name!r}")
        return ExitCode.OK

    _run(go)


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

    async def go() -> ExitCode:
        out = _out()
        target = _require_stand(repo, stand, action="delete")
        config = _load_config(repo)
        projections = await _replay(repo, target)
        control = StandControl(repo=repo, config=config)
        git = GitCli(repo=repo)

        plan = await control.plan_deletion(target, projections, git)
        out.emit(render.deletion_plan(target, plan, force=force))
        if not plan.safe and not force:
            return ExitCode.PARTIAL

        await control.delete(target, projections, git, drop_branches=drop_branches)
        kept = "" if drop_branches else f" ({len(plan.branches)} branch(es) kept)"
        out.line(f"deleted {target}{kept}", style="green")
        return ExitCode.OK

    _run(go)


@app.command
def replay(
    stand: str, *, repo: Path = Path(), kinds: str | None = None, as_json: AsJson = False
) -> None:
    """Print the event log.  Every projection in the system is a fold over this."""

    async def go() -> ExitCode:
        target = _require_stand(repo, stand, action="replay")
        ledger = await SqliteLedger.open(target, _ledger_path(repo, target))
        out = _out()
        try:
            wanted = frozenset(kinds.split(",")) if kinds else None
            for envelope in await ledger.read(kinds=wanted):
                if as_json:
                    out.json(envelope.model_dump(mode="json"))
                else:
                    out.line(render.replay_line(envelope))
        finally:
            await ledger.close()
        return ExitCode.OK

    _run(go)


@app.command
def watch(*, repo: Path = Path(), stand: str | None = None) -> None:
    """Live dashboard: workstream lanes, conflicts, leases, the train, the heat map."""
    from lumberjack.tui.dashboard import run_dashboard

    target = _require_stand(repo, stand, action="watch")
    run_dashboard(repo=repo, stand=target, state_root=_state_root(repo))


@app.command
def serve(*, repo: Path = Path(), stand: str | None = None, transport: str = "stdio") -> None:
    """Serve the coordination toolset over MCP so external agents can join a stand."""
    from lumberjack.server.mcp import serve_stand

    target = _require_stand(repo, stand, action="serve")
    asyncio.run(serve_stand(repo=repo, stand=target, transport=transport))


def main() -> None:
    logging.basicConfig(
        level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s", force=True
    )
    app()


if __name__ == "__main__":
    main()
