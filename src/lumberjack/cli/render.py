"""Everything ``lj`` puts on a screen, as pure functions.

State in, renderables out.  Nothing in this module opens a :class:`rich.console.Console`,
writes to a stream or touches the filesystem -- a renderer that reaches for a console of
its own cannot be tested without capturing a terminal.  The commands in
:mod:`lumberjack.cli.main` do the printing, through :class:`lumberjack.cli.output.Output`,
and the dashboard in :mod:`lumberjack.tui.dashboard` reuses the same functions so the two
surfaces cannot drift into describing the same event differently.

Two conventions hold throughout:

* Ids are truncated to :data:`ID_WIDTH` in tables and never in ``--json`` output.  A
  script that reads an id has to be able to pass it back.
* Colour follows :class:`~lumberjack.domain.conflict.Severity`, not the whim of the
  call site: ``notice`` dim, ``warn`` yellow, ``block`` red.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, assert_never, runtime_checkable

from rich import box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from lumberjack.cli.project import GateDetection
from lumberjack.core.control import DeletionPlan
from lumberjack.core.projections import Projections
from lumberjack.domain.accord import Channel
from lumberjack.domain.claim import Lease
from lumberjack.domain.conflict import ConflictReport, Severity
from lumberjack.domain.events import (
    AccordSigned,
    AmendmentAccepted,
    AmendmentProposed,
    Bounced,
    ChannelClosed,
    ChannelOpened,
    ClaimRequested,
    ComponentFailed,
    ConflictCleared,
    ConflictDetected,
    ContractFrozen,
    DirectiveIssued,
    Envelope,
    EventPayload,
    GateRun,
    LandRequested,
    LeaseDeniedEvent,
    LeaseExpired,
    LeaseGrantedEvent,
    LeaseQueuedEvent,
    LeaseReleased,
    MessageRead,
    MessageSent,
    NegotiationTurn,
    NotePosted,
    ProtocolViolation,
    ResolutionApplied,
    ReviewCommentPosted,
    ReviewCommentResolved,
    StandHalted,
    StandRenamed,
    StandResumed,
    StandStarted,
    TaskAssigned,
    TaskPlanned,
    TaskStateChanged,
    WorkstreamLanded,
    WorktreeDelta,
)
from lumberjack.domain.note import Note, ReviewComment
from lumberjack.domain.request import ModelChoice
from lumberjack.domain.task import TaskGraph
from lumberjack.domain.vcs import MergeOutcome
from lumberjack.domain.workstream import ArbitrationMode, PreservedWorktree
from lumberjack.ids import CommitSha, ConflictId, RepoPath, StandId, TaskId, WorkstreamId

if TYPE_CHECKING:
    # Importing the supervisor pulls in the whole agent stack, which the dashboard and
    # ``lj status`` have no use for.  Only the annotations need it.
    from lumberjack.core.supervisor import StandOutcome

__all__ = [
    "ID_WIDTH",
    "Counters",
    "ExitCode",
    "FeedLine",
    "Progress",
    "TrainEntry",
    "UsageByWorkstream",
    "UsageTotalsLike",
    "board_json",
    "board_view",
    "clip",
    "comment_receipt",
    "comments_json",
    "comments_view",
    "conflict_detail",
    "conflicts_json",
    "conflicts_view",
    "coordination_unavailable",
    "counters_line",
    "degraded_json",
    "degraded_view",
    "deletion_plan",
    "exit_code_for",
    "feed_line",
    "feed_table",
    "heat_map",
    "heat_table",
    "init_report",
    "lifecycle_banner",
    "live_view",
    "models_view",
    "no_stands",
    "outcome_report",
    "plain_feed_line",
    "plan_view",
    "preserved_report",
    "progress_rows",
    "progress_table",
    "promote_report",
    "replay_line",
    "resume_bases",
    "run_banner",
    "severity_style",
    "short",
    "state_style",
    "status_json",
    "status_view",
    "train_entries",
    "train_table",
    "usage_error",
]

ID_WIDTH = 8
"""How much of an identifier a table shows.  ``--json`` shows all of it."""

SEVERITY_STYLE: Mapping[Severity, str] = {
    Severity.NOTICE: "dim",
    Severity.WARN: "yellow",
    Severity.BLOCK: "bold red",
}

LIFECYCLE_STYLE: Mapping[str, str] = {
    "live": "bold green",
    "stale": "bold red",
    "halted": "bold red",
    "finished": "bold yellow",
}

LIFECYCLE_NOTE: Mapping[str, str] = {
    "stale": "the supervisor that started this run is gone; nothing below is running",
    "halted": "this stand was halted; nothing below is running",
    "finished": "every task is terminal; nothing below is running",
}

TERMINAL_STATES = frozenset({"landed", "blocked", "abandoned"})

COUNTED_KINDS = frozenset({"worktree_delta", "claim_requested", "message_read"})
"""Kinds that fire constantly.  They belong in a counter, not in a feed."""


class ExitCode(IntEnum):
    """What ``lj`` returns to the shell.

    CI reads these, so they are part of the interface rather than an implementation
    detail: a run that blocked a task has to be distinguishable from one that did not.
    """

    OK = 0
    PARTIAL = 1
    USAGE = 2
    NO_STAND = 3


@runtime_checkable
class UsageTotalsLike(Protocol):
    """The shape ``0002``'s ``UsageTotals`` exposes, as much of it as display needs.

    Structural on purpose: this module renders token counts without importing the
    telemetry package, so the two specs can land in either order.  The members are
    read-only so a pydantic field and a computed property both satisfy it.
    """

    @property
    def input_tokens(self) -> int: ...
    @property
    def output_tokens(self) -> int: ...
    @property
    def total_tokens(self) -> int: ...
    @property
    def requests(self) -> int: ...


UsageByWorkstream = Mapping[WorkstreamId, UsageTotalsLike]


def short(value: str, width: int = ID_WIDTH) -> str:
    """Truncate an identifier for a table.  Never used on ``--json`` output."""
    return value[:width]


def severity_style(severity: Severity) -> str:
    return SEVERITY_STYLE.get(severity, "")


def clip(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return text[: max(0, width - 1)] + "…"


# -- the live view during ``lj run`` --------------------------------------------------


@dataclass(frozen=True, slots=True)
class FeedLine:
    """One event, as a person would describe it."""

    at: datetime
    style: str
    text: str


@dataclass(frozen=True, slots=True)
class Progress:
    """One workstream's lane in the live view."""

    workstream: WorkstreamId
    agent: str
    task: str
    title: str
    state: str
    files: int
    lines_changed: int
    bounces: int
    present: bool = True
    tokens: int | None = None

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES


@dataclass(frozen=True, slots=True)
class Counters:
    """Event kinds too noisy for the feed, kept as running totals."""

    counts: Mapping[str, int]

    def with_event(self, kind: str) -> Counters:
        counts = dict(self.counts)
        counts[kind] = counts.get(kind, 0) + 1
        return Counters(counts=counts)


def feed_line(envelope: Envelope[EventPayload]) -> FeedLine | None:
    """One line for an event a person cares about, or ``None`` for the noise.

    The match is exhaustive against :data:`~lumberjack.domain.events.EventPayload`, so
    a new event kind is a type error here rather than an event that silently never
    reaches the screen.
    """
    payload = envelope.payload
    at = envelope.at
    match payload:
        case StandStarted():
            return FeedLine(at, "bold", f"stand {payload.stand} on {payload.integration_branch}")
        case StandResumed():
            carried = len(payload.carried)
            return FeedLine(at, "bold", f"session {payload.session} resumed, {carried} carried")
        case StandRenamed():
            return FeedLine(at, "dim", f"renamed to {payload.name!r}")
        case StandHalted():
            return FeedLine(at, "bold red", f"halted: {payload.reason}")
        case ComponentFailed():
            # A component that keeps retrying is a warning; one that gave up is the
            # reason the rest of the screen has stopped making sense.
            style = "bold red" if payload.giving_up else "yellow"
            return FeedLine(at, style, payload.summary())
        case TaskPlanned():
            return FeedLine(at, "dim", f"planned {payload.spec.task_id}: {payload.spec.title}")
        case TaskAssigned():
            agent = payload.workstream.agent
            return FeedLine(at, "", f"{agent} takes {payload.task}")
        case TaskStateChanged():
            style = {"blocked": "bold red", "landed": "green"}.get(payload.to, "")
            detail = f" -- {payload.detail}" if payload.detail else ""
            return FeedLine(at, style, f"{payload.task_id}: {payload.frm} -> {payload.to}{detail}")
        case ClaimRequested():
            return None
        case LeaseGrantedEvent():
            lease = payload.lease
            where = clip(lease.scope.describe(), 48)
            if payload.coexisting:
                peers = ", ".join(payload.coexisting)
                return FeedLine(
                    at,
                    "yellow",
                    f"{lease.holder} claims {where} ({lease.mode.value}) alongside {peers}",
                )
            return FeedLine(at, "", f"{lease.holder} claims {where} ({lease.mode.value})")
        case LeaseQueuedEvent():
            blockers = ", ".join(payload.blockers) or "?"
            return FeedLine(
                at,
                "yellow",
                f"{payload.claim.claimant} queued at {payload.position} behind {blockers}",
            )
        case LeaseDeniedEvent():
            return FeedLine(
                at, "bold red", f"{payload.claim.claimant} denied: {payload.reason.value}"
            )
        case LeaseReleased():
            return FeedLine(at, "dim", f"{payload.holder} released {short(payload.lease_id)}")
        case LeaseExpired():
            return FeedLine(at, "yellow", f"{payload.holder}'s lease expired")
        case WorktreeDelta():
            return None
        case ProtocolViolation():
            return FeedLine(
                at,
                severity_style(payload.severity),
                f"{payload.by} {payload.violation.code}: {payload.violation.detail}",
            )
        case ConflictDetected():
            report = payload.report
            return FeedLine(at, severity_style(report.severity), report.summary())
        case ConflictCleared():
            left, right = payload.between
            why = f" -- {payload.why}" if payload.why else ""
            return FeedLine(
                at, "green", f"cleared {short(payload.conflict_id)}: {left}/{right}{why}"
            )
        case ChannelOpened():
            who = ", ".join(sorted(payload.participants))
            return FeedLine(at, "yellow", f"negotiation opened between {who}")
        case NegotiationTurn():
            move = payload.move
            proposes = f" [proposes {move.propose.kind}]" if move.propose is not None else ""
            signs = " [signs]" if move.sign else ""
            return FeedLine(at, "dim", f"{move.by}: {clip(move.say, 60)}{proposes}{signs}")
        case ChannelClosed():
            return FeedLine(at, "", f"negotiation {payload.state}")
        case AccordSigned():
            accord = payload.accord
            who = ", ".join(sorted(accord.signed_by))
            return FeedLine(at, "green", f"accord: {accord.resolution.kind} signed by {who}")
        case DirectiveIssued():
            directive = payload.directive
            return FeedLine(
                at, "yellow", f"foreman directs {directive.resolution.kind}: {directive.rationale}"
            )
        case ResolutionApplied():
            return FeedLine(
                at,
                "green",
                f"applied {payload.resolution.kind} to {short(payload.conflict_id)} "
                f"by {payload.source}",
            )
        case NotePosted():
            note = payload.note
            return FeedLine(at, "dim", f"note [{note.topic}] {note.author}: {clip(note.body, 60)}")
        case ReviewCommentPosted():
            comment = payload.comment
            return FeedLine(at, "yellow", f"review on {comment.file}:{comment.lines}")
        case ReviewCommentResolved():
            return FeedLine(at, "green", f"review {short(payload.comment_id)} resolved")
        case MessageSent():
            message = payload.message
            return FeedLine(at, "dim", f"{message.frm} -> {message.to}: {message.subject}")
        case MessageRead():
            return None
        case ContractFrozen():
            contract = payload.contract
            return FeedLine(at, "", f"contract {short(contract.contract_id)} frozen")
        case AmendmentProposed():
            return FeedLine(at, "yellow", f"amendment proposed by {payload.proposal.proposer}")
        case AmendmentAccepted():
            return FeedLine(at, "green", f"amendment accepted by {payload.accepted_by}")
        case LandRequested():
            return FeedLine(
                at, "", f"{payload.workstream} requests landing at position {payload.position}"
            )
        case GateRun():
            failure = payload.report.first_failure
            if failure is None:
                return FeedLine(at, "green", f"{payload.workstream}: gate passed")
            return FeedLine(
                at,
                "bold red",
                f"{payload.workstream}: gate failed at {failure.name} (exit {failure.exit_code})",
            )
        case WorkstreamLanded():
            return FeedLine(
                at, "bold green", f"{payload.task} landed at {short(payload.integration_head)}"
            )
        case Bounced():
            return FeedLine(
                at, "bold red", f"{payload.task} bounced (attempt {payload.attempt}): {payload.why}"
            )
        case _ as unreachable:
            assert_never(unreachable)


def plain_feed_line(line: FeedLine) -> str:
    """The non-TTY form: one line per event, no cursor games, safe to redirect."""
    return f"{line.at:%H:%M:%S}  {line.text}"


def progress_rows(
    projections: Projections,
    *,
    present: Mapping[WorkstreamId, bool] | None = None,
    usage: UsageByWorkstream | None = None,
) -> tuple[Progress, ...]:
    """One :class:`Progress` per workstream, ordered as the ledger assigned them."""
    rows: list[Progress] = []
    for item in projections.workstreams.values():
        task = projections.tasks.get(item.task)
        delta = projections.deltas.get(item.workstream_id)
        totals = usage.get(item.workstream_id) if usage is not None else None
        rows.append(
            Progress(
                workstream=item.workstream_id,
                agent=str(item.agent),
                task=str(item.task),
                title=task.spec.title if task is not None else str(item.task),
                state=task.kind if task is not None else "unknown",
                files=len(delta.paths) if delta is not None else 0,
                lines_changed=delta.lines_changed if delta is not None else 0,
                bounces=projections.bounces.get(item.workstream_id, 0),
                present=True if present is None else present.get(item.workstream_id, True),
                tokens=totals.total_tokens if totals is not None else None,
            )
        )
    return tuple(rows)


def progress_table(rows: Sequence[Progress]) -> Table:
    """The per-workstream lanes.  The token column appears only once 0002 is wired in."""
    show_tokens = any(row.tokens is not None for row in rows)
    table = Table(box=box.SIMPLE, expand=True, pad_edge=False)
    table.add_column("agent", no_wrap=True)
    table.add_column("task", overflow="ellipsis")
    table.add_column("state", no_wrap=True)
    table.add_column("files", justify="right", no_wrap=True)
    table.add_column("lines", justify="right", no_wrap=True)
    table.add_column("bounces", justify="right", no_wrap=True)
    if show_tokens:
        table.add_column("tokens", justify="right", no_wrap=True)
    for row in rows:
        # A finished lane, or one whose worktree is gone, is history. Rendering it
        # exactly like a working agent is how a dead stand comes to look like a live one.
        style = "dim" if row.terminal or not row.present else ""
        cells = [
            Text(row.agent, style=style),
            Text(row.title, style=style),
            Text(row.state, style=style or state_style(row.state)),
            Text(str(row.files), style=style),
            Text(str(row.lines_changed), style=style),
            Text(str(row.bounces) if row.bounces else "-", style=style or _bounce_style(row)),
        ]
        if show_tokens:
            cells.append(Text("-" if row.tokens is None else f"{row.tokens:,}", style=style))
        table.add_row(*cells)
    return table


def state_style(state: str) -> str:
    return {"landed": "green", "blocked": "bold red", "running": "cyan"}.get(state, "")


def _bounce_style(row: Progress) -> str:
    return "yellow" if row.bounces else "dim"


def feed_table(lines: Sequence[FeedLine]) -> Table:
    table = Table(box=None, show_header=False, expand=True, pad_edge=False)
    table.add_column("at", no_wrap=True, style="dim")
    table.add_column("what", overflow="ellipsis")
    for line in lines:
        table.add_row(f"{line.at:%H:%M:%S}", Text(line.text, style=line.style))
    return table


def counters_line(counters: Counters) -> Text:
    """The noisy kinds, as totals.  ``worktree_delta`` fires constantly; this is where."""
    if not counters.counts:
        return Text("no file activity yet", style="dim")
    parts = ", ".join(f"{kind} {count}" for kind, count in sorted(counters.counts.items()))
    return Text(parts, style="dim")


def lifecycle_banner(lifecycle: str) -> Text:
    return Text(lifecycle.upper(), style=LIFECYCLE_STYLE.get(lifecycle, ""))


def run_banner(*, stand: StandId, repo: Path, runtime: str, parallel: int) -> Text:
    banner = Text()
    banner.append("stand ", style="dim")
    banner.append(str(stand), style="bold")
    banner.append(f" on {repo}\n", style="dim")
    banner.append(f"runtime: {runtime}, up to {parallel} parallel", style="dim")
    return banner


def live_view(
    *,
    title: str,
    lifecycle: str,
    integration: str,
    head: str | None,
    rows: Sequence[Progress],
    feed: Sequence[FeedLine],
    counters: Counters,
) -> RenderableType:
    """The whole live view: a lane per workstream, then the feed, then the counters."""
    heading = Text()
    heading.append(clip(title, 60), style="bold")
    heading.append("  ")
    heading.append_text(lifecycle_banner(lifecycle))
    heading.append(f"\n{integration} @ {short(head or '?')}", style="dim")
    return Group(
        Panel(heading, box=box.ROUNDED, expand=True),
        progress_table(rows),
        Panel(feed_table(feed), title="events", box=box.ROUNDED, expand=True),
        counters_line(counters),
    )


def outcome_report(
    outcome: StandOutcome, *, usage: UsageTotalsLike | None = None
) -> RenderableType:
    """What ``lj run`` leaves on the screen after the swarm stops."""
    style = "bold green" if outcome.status == "completed" else "bold yellow"
    if outcome.status == "failed":
        style = "bold red"
    head = Text(outcome.summary(), style=style)

    table = Table(box=box.SIMPLE, expand=True, pad_edge=False)
    table.add_column("agent", no_wrap=True)
    table.add_column("state", no_wrap=True)
    table.add_column("task", overflow="ellipsis")
    for item in outcome.workstreams:
        table.add_row(
            str(item.agent),
            Text(item.task.kind, style=state_style(item.task.kind)),
            item.task.spec.title,
        )

    parts: list[RenderableType] = [head, table]
    if usage is not None:
        parts.append(
            Text(
                f"tokens: {usage.total_tokens:,} "
                f"({usage.input_tokens:,} in, {usage.output_tokens:,} out) "
                f"over {usage.requests:,} request(s)",
                style="dim",
            )
        )
    # Which worktrees survive, and why, is decided by teardown -- after this report is
    # built.  :func:`preserved_report` says it once the stand has actually closed.
    return Group(*parts)


def exit_code_for(outcome: StandOutcome) -> ExitCode:
    """A run that blocked a task exits non-zero, or it cannot be used in CI."""
    if outcome.status == "completed" and not outcome.blocked:
        return ExitCode.OK
    return ExitCode.PARTIAL


PRESERVED_HEADING: Mapping[str, str] = {
    "unlanded": "preserved worktrees (unlanded work)",
    "halted": "preserved worktrees (the stand halted)",
    "cleanup_failed": "worktrees left behind -- removal failed, delete these by hand",
}

PRESERVED_STYLE: Mapping[str, str] = {
    "unlanded": "yellow",
    "halted": "yellow",
    "cleanup_failed": "bold red",
}


def preserved_report(preserved: Sequence[PreservedWorktree]) -> RenderableType | None:
    """Worktrees still on disk after teardown, grouped by *why* they are still there.

    "Kept because it holds work that never landed" is the harness behaving; "kept
    because removing it failed" is a directory the operator now owns.  One heading over
    both is how the second stayed invisible, so each reason gets its own, and the
    failures are red because they are the only ones that ask for action.
    """
    if not preserved:
        return None
    parts: list[RenderableType] = []
    for reason in ("cleanup_failed", "unlanded", "halted"):
        group = [item for item in preserved if item.reason == reason]
        if not group:
            continue
        block = Text(f"{PRESERVED_HEADING[reason]}:", style=PRESERVED_STYLE[reason])
        for item in group:
            block.append(f"\n  {item.render()}", style="dim")
        parts.append(block)
    return Group(*parts)


# -- ``lj status`` ---------------------------------------------------------------------


def status_view(
    projections: Projections,
    *,
    stand: StandId,
    lifecycle: str,
    now: datetime,
    present: Mapping[WorkstreamId, bool] | None = None,
    usage: UsageByWorkstream | None = None,
) -> RenderableType:
    heading = Text()
    heading.append(f"stand {stand} ", style="bold")
    heading.append_text(lifecycle_banner(lifecycle))
    heading.append(f": {projections.title}")
    note = LIFECYCLE_NOTE.get(lifecycle)
    if note is not None:
        heading.append(f"\n{note}", style="dim")

    landed = len(projections.landed)
    moved = projections.integration_head != projections.base
    heading.append(
        f"\nintegration: {projections.integration_branch} @ "
        f"{short(projections.integration_head or '?')}",
        style="dim",
    )
    # An integration head with no indication of whether anything landed is a number a
    # person cannot act on: base and head look identical when the run achieved nothing.
    if landed:
        heading.append(f"  ({landed} task(s) landed)", style="green")
    elif moved:
        heading.append("  (moved, but no task landed)", style="yellow")
    else:
        heading.append("  (nothing has landed yet)", style="yellow")

    rows = progress_rows(projections, present=present, usage=usage)
    parts: list[RenderableType] = [heading]
    # Above the workstreams, not below them: a stand whose oracle has stopped renders
    # an empty conflicts table, and that reads as "all clear" unless it says otherwise.
    failures = degraded_view(projections)
    if failures is not None:
        parts.append(failures)
    parts += [
        Text(f"\nworkstreams ({len(rows)})", style="bold"),
        progress_table(rows),
        Text(f"leases ({len(projections.active_leases(now))})", style="bold"),
        _lease_table(projections.active_leases(now)),
        Text(f"open conflicts ({len(projections.conflicts)})", style="bold"),
        conflicts_view(projections),
    ]
    entries = train_entries(projections)
    if entries:
        parts.extend([Text("merge train", style="bold"), train_table(entries)])
    return Group(*parts)


def degraded_view(projections: Projections) -> RenderableType | None:
    """The parts of the harness that are failing, or ``None`` when none are.

    A stand whose oracle raises on every probe stops detecting conflicts and otherwise
    looks exactly like a healthy one: same lanes, same empty conflict table.  This is
    the pane that tells the difference, so it leads rather than trails.
    """
    failures = tuple(failure for _, failure in sorted(projections.degraded.items()))
    if not failures:
        return None
    table = Table(box=box.SIMPLE, expand=True, pad_edge=False)
    table.add_column("component", no_wrap=True)
    table.add_column("state", no_wrap=True)
    table.add_column("last error", overflow="ellipsis")
    for failure in failures:
        style = "bold red" if failure.giving_up else "yellow"
        state = "stopped" if failure.giving_up else f"retrying ({failure.consecutive})"
        table.add_row(
            Text(failure.component, style=style),
            Text(state, style=style),
            Text(clip(failure.error, 70), style="dim"),
        )
    stopped = [failure for failure in failures if failure.giving_up]
    heading = Text("degraded components", style="bold red" if stopped else "bold yellow")
    if stopped:
        heading.append(
            "\nwhat these components report has stopped updating; the rest of this"
            " view is only as current as they were",
            style="red",
        )
    refs = [failure.traceback_ref for failure in failures if failure.traceback_ref]
    parts: list[RenderableType] = [heading, table]
    if refs:
        # The event carries a one-line reason; the artifact carries the traceback.
        trail = Text("tracebacks:", style="dim")
        for ref in refs:
            trail.append(f"\n  {ref}", style="dim")
        parts.append(trail)
    return Group(*parts)


def degraded_json(projections: Projections) -> list[dict[str, Any]]:
    """:func:`degraded_view` for scripts, identifiers and refs left whole."""
    return [
        {
            "component": failure.component,
            "error": failure.error,
            "consecutive": failure.consecutive,
            "giving_up": failure.giving_up,
            "traceback_ref": failure.traceback_ref,
            "workstream": str(failure.workstream) if failure.workstream else None,
        }
        for _, failure in sorted(projections.degraded.items())
    ]


def _lease_table(leases: Sequence[Lease]) -> Table:
    table = Table(box=box.SIMPLE, expand=True, pad_edge=False)
    table.add_column("holder", no_wrap=True)
    table.add_column("mode", no_wrap=True)
    table.add_column("scope", overflow="ellipsis")
    if not leases:
        table.add_row(Text("none", style="dim"), "", "")
        return table
    for lease in leases:
        table.add_row(str(lease.holder), lease.mode.value, lease.scope.describe())
    return table


def status_json(
    projections: Projections,
    *,
    stand: StandId,
    lifecycle: str,
    now: datetime,
    present: Mapping[WorkstreamId, bool] | None = None,
    usage: UsageByWorkstream | None = None,
) -> dict[str, Any]:
    """The same state as :func:`status_view`, with identifiers left whole."""
    rows = progress_rows(projections, present=present, usage=usage)
    return {
        "stand": str(stand),
        "title": projections.title,
        "lifecycle": lifecycle,
        "goal": projections.goal,
        "session": projections.session,
        "integration": {
            "branch": projections.integration_branch,
            "head": projections.integration_head,
            "base": projections.base,
            "landed": len(projections.landed),
        },
        "workstreams": [
            {
                "workstream": str(row.workstream),
                "agent": row.agent,
                "task": row.task,
                "title": row.title,
                "state": row.state,
                "files": row.files,
                "lines_changed": row.lines_changed,
                "bounces": row.bounces,
                "worktree_present": row.present,
                "tokens": row.tokens,
            }
            for row in rows
        ],
        "leases": [
            {
                "lease": str(lease.lease_id),
                "holder": str(lease.holder),
                "workstream": str(lease.workstream),
                "mode": lease.mode.value,
                "scope": lease.scope.describe(),
            }
            for lease in projections.active_leases(now)
        ],
        "conflicts": conflicts_json(projections),
        "degraded": degraded_json(projections),
        "train": [
            {
                "position": entry.position,
                "workstream": str(entry.workstream),
                "agent": entry.agent,
                "stage": entry.stage,
            }
            for entry in train_entries(projections)
        ],
    }


# -- ``lj conflicts`` ------------------------------------------------------------------


def conflicts_view(
    projections: Projections, *, explain: ConflictId | None = None
) -> RenderableType:
    reports = [
        report
        for report in projections.conflicts.values()
        if explain is None or report.conflict_id == explain
    ]
    if not reports:
        missing = "no such conflict" if explain is not None else "no open conflicts"
        return Text(missing, style="dim")
    if explain is not None:
        channels = projections.channels.values()
        return Group(*(conflict_detail(report, channels) for report in reports))

    table = Table(box=box.SIMPLE, expand=True, pad_edge=False)
    table.add_column("id", no_wrap=True)
    table.add_column("severity", no_wrap=True)
    table.add_column("source", no_wrap=True)
    table.add_column("between", overflow="ellipsis")
    table.add_column("files", overflow="ellipsis")
    for report in reports:
        table.add_row(
            short(report.conflict_id),
            Text(report.severity.label, style=severity_style(report.severity)),
            report.source.value,
            " vs ".join(report.between),
            ", ".join(sorted(report.paths)[:3]) or "-",
        )
    return table


def conflict_detail(report: ConflictReport, channels: Iterable[Channel] = ()) -> RenderableType:
    """The single most informative thing the system knows about a pair of agents.

    Evidence, the conflicting hunks, and whatever the two of them have said to each
    other so far.  The dashboard shows this in a pane; ``lj conflicts --explain`` prints
    the same thing.
    """
    heading = Text()
    heading.append(f"{report.severity.label} ", style=severity_style(report.severity))
    heading.append(f"{report.source.value}  ")
    heading.append(" vs ".join(report.between), style="bold")
    heading.append(f"\n{report.conflict_id}  detected {report.detected_at:%H:%M:%S}", style="dim")

    files = Table(box=box.SIMPLE, expand=True, pad_edge=False)
    files.add_column("file", overflow="ellipsis")
    files.add_column("stage", no_wrap=True)
    files.add_column("hunks", justify="right", no_wrap=True)
    files.add_column("symbols", overflow="ellipsis")
    for item in report.files:
        files.add_row(
            item.path,
            item.stage.value,
            str(item.hunks),
            ", ".join(str(symbol) for symbol in item.symbols[:4]) or "-",
        )

    parts: list[RenderableType] = [heading, files]
    if report.evidence:
        parts.append(
            Panel(Text(report.evidence, style="dim"), title="evidence", box=box.ROUNDED)
        )
    transcript = _transcript_for(report.conflict_id, channels)
    if transcript is not None:
        parts.append(Panel(transcript, title="negotiation", box=box.ROUNDED))
    return Group(*parts)


def _transcript_for(conflict: ConflictId, channels: Iterable[Channel]) -> Text | None:
    for channel in channels:
        if channel.conflict_id != conflict:
            continue
        body = Text(channel.transcript() or "(no turns yet)")
        body.append(
            f"\n-- {channel.state.value}, {channel.turns_used} turn(s) used", style="dim"
        )
        return body
    return None


def conflicts_json(projections: Projections) -> list[dict[str, Any]]:
    return [
        {
            "conflict": str(report.conflict_id),
            "between": [str(item) for item in report.between],
            "source": report.source.value,
            "severity": report.severity.label,
            "detected_at": report.detected_at.isoformat(),
            "evidence": report.evidence,
            "files": [
                {
                    "path": item.path,
                    "stage": item.stage.value,
                    "hunks": item.hunks,
                    "symbols": [str(symbol) for symbol in item.symbols],
                }
                for item in report.files
            ],
            "transcript": _transcript_text(report.conflict_id, projections.channels.values()),
        }
        for report in projections.conflicts.values()
    ]


def _transcript_text(conflict: ConflictId, channels: Iterable[Channel]) -> str | None:
    for channel in channels:
        if channel.conflict_id == conflict:
            return channel.transcript()
    return None


# -- ``lj board`` ----------------------------------------------------------------------


def board_view(notes: Sequence[Note]) -> RenderableType:
    if not notes:
        return Text("the blackboard is empty", style="dim")
    table = Table(box=box.SIMPLE, expand=True, pad_edge=False)
    table.add_column("topic", no_wrap=True)
    table.add_column("author", no_wrap=True)
    table.add_column("scope", overflow="ellipsis")
    table.add_column("body", overflow="fold")
    for note in notes:
        table.add_row(
            note.topic,
            str(note.author),
            note.scope.describe() if note.scope is not None else "-",
            note.body,
        )
    return table


def board_json(notes: Sequence[Note]) -> list[dict[str, Any]]:
    return [
        {
            "note": str(note.note_id),
            "topic": note.topic,
            "author": str(note.author),
            "scope": note.scope.describe() if note.scope is not None else None,
            "body": note.body,
            "posted_at": note.posted_at.isoformat(),
        }
        for note in notes
    ]


# -- ``lj replay`` ---------------------------------------------------------------------


def replay_line(envelope: Envelope[EventPayload]) -> str:
    return f"{envelope.seq:>5} {envelope.at:%H:%M:%S} {envelope.actor!s:<24} {envelope.kind}"


# -- the merge train and the heat map --------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrainEntry:
    position: int
    workstream: WorkstreamId
    agent: str
    stage: str
    """Where this entry got to: queued, or the gate check it is on."""


def train_entries(projections: Projections) -> tuple[TrainEntry, ...]:
    """The queue, with the leading entry's gate stage read off its last gate run."""
    entries: list[TrainEntry] = []
    for position, workstream in enumerate(projections.train, start=1):
        agent = projections.agent_of(workstream)
        report = projections.gate_reports.get(workstream)
        if report is None:
            stage = "queued" if position > 1 else "waiting for the gate"
        else:
            failure = report.report.first_failure
            stage = f"gate: {failure.name} failed" if failure else "gate passed"
        entries.append(
            TrainEntry(
                position=position,
                workstream=workstream,
                agent=str(agent) if agent is not None else "?",
                stage=stage,
            )
        )
    return tuple(entries)


def train_table(entries: Sequence[TrainEntry]) -> Table:
    table = Table(box=box.SIMPLE, expand=True, pad_edge=False)
    table.add_column("#", justify="right", no_wrap=True)
    table.add_column("agent", no_wrap=True)
    table.add_column("stage", overflow="ellipsis")
    if not entries:
        table.add_row(Text("empty", style="dim"), "", "")
        return table
    for entry in entries:
        style = "bold" if entry.position == 1 else ""
        table.add_row(
            Text(str(entry.position), style=style),
            Text(entry.agent, style=style),
            Text(entry.stage, style="bold red" if "failed" in entry.stage else style),
        )
    return table


def heat_map(projections: Projections, *, limit: int = 20) -> tuple[tuple[RepoPath, int], ...]:
    """Paths by how many workstreams are touching them, hottest first.

    This is the view that tells an operator the task decomposition was wrong: a file
    that three agents are all editing was never three tasks.
    """
    counts: dict[RepoPath, int] = {}
    for workstream in projections.workstreams:
        for path in projections.observed_paths(workstream):
            counts[path] = counts.get(path, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return tuple(ranked[:limit])


def heat_table(rows: Sequence[tuple[RepoPath, int]]) -> Table:
    table = Table(box=box.SIMPLE, expand=True, pad_edge=False)
    table.add_column("path", overflow="ellipsis")
    table.add_column("workstreams", justify="right", no_wrap=True)
    if not rows:
        table.add_row(Text("nothing observed yet", style="dim"), "")
        return table
    for path, count in rows:
        style = "bold red" if count > 2 else ("yellow" if count > 1 else "")
        table.add_row(Text(path, style=style), Text(str(count), style=style))
    return table


# -- the small sharp edges -------------------------------------------------------------


def no_stands(repo: Path, *, action: str = "run") -> Text:
    """Every ``no stands found`` path says what to do next, rather than stopping dead."""
    message = Text()
    message.append(f"no stands found under {repo}\n", style="bold yellow")
    message.append("start one with:  ", style="dim")
    message.append('lj run "what you want built"')
    message.append(f"\nor name an existing one:  lj {action} --stand <stand-id>", style="dim")
    return message


def usage_error(detail: str) -> Text:
    """A command that was asked for something it cannot do.  Exit code 2."""
    message = Text()
    message.append("usage: ", style="bold red")
    message.append(detail)
    return message


def coordination_unavailable(error: Exception, *, verb: str = "start") -> Text:
    """Why refusing to run is the right answer when the coordination server is down.

    Without it the agents would run without claims, awareness or conflict checks --
    writing code blind while the harness reported progress.
    """
    message = Text()
    message.append(f"coordination is unavailable, so the stand did not {verb}:\n", style="bold red")
    message.append(f"  {error}\n\n", style="red")
    message.append(
        "Agents would have run without claims, awareness or conflict checks -- writing "
        "code blind while this reported progress.",
        style="dim",
    )
    return message


def init_report(
    *,
    config_path: Path,
    state_root: Path,
    detection: GateDetection,
    arbitration: ArbitrationMode,
    max_parallel: int,
) -> RenderableType:
    """What ``lj init`` wrote, and -- when the gate is empty -- what to write next.

    ``lumberjack.json`` is strict JSON with no room for a comment, and the MCP server
    validates it with ``extra="forbid"``, so the explanation for an empty gate belongs
    on the screen at the moment it is written rather than inside the file.
    """
    body = Text()
    body.append(f"wrote {config_path}\n")
    body.append(f"state  {state_root}\n", style="dim")
    body.append(
        f"arbitration: {arbitration.value}, up to {max_parallel} parallel workstreams\n",
        style="dim",
    )
    body.append(f"project: {detection.project} -- {detection.why}\n", style="dim")
    if detection.empty:
        body.append(
            "\nthe gate is empty: nothing is checked before an agent lands.", style="yellow"
        )
        return Group(body, Panel(Text(EMPTY_GATE_HINT), title="fill this in", box=box.ROUNDED))
    gate = Table(box=box.SIMPLE, pad_edge=False)
    gate.add_column("gate", no_wrap=True)
    for command in detection.commands:
        gate.add_row(" ".join(command))
    return Group(body, gate)


EMPTY_GATE_HINT = (
    'Add the commands that must pass before an agent may land, as "gate_commands" in\n'
    "lumberjack.json:\n\n"
    '  "gate_commands": [["make", "lint"], ["make", "test"]]\n\n'
    "Each entry is one command as a list of arguments; they run in order, in the\n"
    "agent's worktree, and the first failure bounces the workstream."
)


def plan_view(graph: TaskGraph) -> RenderableType:
    """The task graph in waves, which is the order the supervisor will schedule them."""
    parts: list[RenderableType] = []
    for layer, wave in enumerate(graph.topological_layers(), start=1):
        table = Table(box=box.SIMPLE, expand=True, pad_edge=False, title=f"wave {layer}")
        table.title_justify = "left"
        table.add_column("task", no_wrap=True)
        table.add_column("title", overflow="ellipsis")
        table.add_column("scope", overflow="ellipsis")
        for spec in wave:
            table.add_row(
                str(spec.task_id),
                spec.title,
                spec.predicted_scope.describe() if spec.predicted_scope else "?",
            )
        parts.append(table)
    parts.append(Text("nothing has run; `lj run` executes this", style="dim"))
    return Group(*parts)


def resume_bases(found: Mapping[TaskId, CommitSha]) -> RenderableType:
    table = Table(box=box.SIMPLE, expand=True, pad_edge=False)
    table.add_column("task", no_wrap=True)
    table.add_column("resuming from", no_wrap=True)
    for task, commit in found.items():
        table.add_row(str(task), short(commit))
    return table


def models_view(choices: Sequence[ModelChoice]) -> RenderableType:
    table = Table(box=box.SIMPLE, expand=True, pad_edge=False)
    table.add_column("model", no_wrap=True)
    table.add_column("label", no_wrap=True)
    table.add_column("note", overflow="ellipsis")
    for item in choices:
        mark = " (default)" if item.default else ""
        style = "bold" if item.default else ""
        table.add_row(Text(item.qualified + mark, style=style), item.label, item.note)
    return table


def comments_view(items: Sequence[ReviewComment]) -> RenderableType:
    if not items:
        return Text("no review comments", style="dim")
    table = Table(box=box.SIMPLE, expand=True, pad_edge=False)
    table.add_column("id", no_wrap=True)
    table.add_column("state", no_wrap=True)
    table.add_column("where", no_wrap=True)
    table.add_column("body", overflow="fold")
    for item in items:
        open_style = "dim" if item.resolved else "yellow"
        table.add_row(
            short(item.comment_id),
            Text("resolved" if item.resolved else "open", style=open_style),
            f"{item.file}:{item.lines}",
            item.body,
        )
    return table


def comments_json(items: Sequence[ReviewComment]) -> list[dict[str, Any]]:
    return [
        {
            "comment": str(item.comment_id),
            "author": str(item.author),
            "file": item.file,
            "lines": item.lines,
            "body": item.body,
            "workstream": str(item.workstream) if item.workstream else None,
            "conflict": str(item.conflict_id) if item.conflict_id else None,
            "resolved": item.resolved,
            "posted_at": item.posted_at.isoformat(),
        }
        for item in items
    ]


def comment_receipt(comment: ReviewComment, recipients: Sequence[str]) -> RenderableType:
    body = Text()
    body.append(f"{comment.comment_id} on {comment.file}:{comment.lines}\n")
    body.append("delivered to: ", style="dim")
    body.append(", ".join(recipients) or "nobody active", style="" if recipients else "yellow")
    return body


def promote_report(outcome: MergeOutcome, *, destination: str) -> RenderableType:
    style = "green" if outcome.ok else "bold red"
    body = Text()
    body.append(f"{outcome.status}", style=style)
    body.append(f" -> {destination} @ {short(outcome.head or '?')}")
    if outcome.conflicted:
        body.append("\nconflicted: " + ", ".join(outcome.conflicted), style="bold red")
    if outcome.detail:
        body.append(f"\n{outcome.detail}", style="dim")
    return body


def deletion_plan(stand: StandId, plan: DeletionPlan, *, force: bool) -> RenderableType:
    """What deleting would destroy, and why it is refusing when it refuses."""
    body = Text()
    body.append(f"{stand}: {plan.describe()}\n")
    if plan.running and not force:
        body.append("it is still running. Halt it first, or pass --force.\n", style="bold red")
    for branch in plan.unlanded:
        body.append(f"holds unlanded work: {branch}\n", style="yellow")
    if plan.unlanded and not force:
        body.append(
            "pass --force to delete anyway; branches are kept unless --drop-branches.",
            style="dim",
        )
    return body
