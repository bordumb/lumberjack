"""``lj watch``: a live view of the stand.

Everything here is a fold over the ledger, so the dashboard is a reader like any
other -- it never talks to the coordinator, and attaching one cannot perturb a run.

The panes are ordered by how much they tell an operator who is trying to decide
whether to intervene: the conflict detail first, because the oracle's evidence and the
negotiation transcript are the most informative thing the system knows; then the merge
train; then the event feed; then the file heat map, which is the view that says the task
decomposition was wrong.  Every one of them renders through
:mod:`lumberjack.cli.render`, so the dashboard and the command line cannot drift into
describing the same event two different ways.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from pathlib import Path
from typing import ClassVar

from rich.console import RenderableType
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)

from lumberjack.adapters.clock import SystemClock
from lumberjack.adapters.sqlite_ledger import SqliteLedger
from lumberjack.cli import render
from lumberjack.cli.render import Counters, FeedLine
from lumberjack.core.control import stand_alive
from lumberjack.core.projections import Projections
from lumberjack.ids import ConflictId, StandId, WorkstreamId

__all__ = ["Dashboard", "run_dashboard"]

FEED_DEPTH = 400
"""How much history the feed keeps.  Older than this is what ``lj replay`` is for."""


class Dashboard(App[None]):
    """Workstream lanes, live claims, conflicts, the merge train and the heat map."""

    CSS = """
    Screen { layout: vertical; }
    #headline { height: auto; padding: 0 1; }
    #detail { padding: 0 1; }
    #filter { display: none; }
    #filter.showing { display: block; }
    DataTable { height: 1fr; }
    #conflict-list { width: 45%; }
    """
    BINDINGS: ClassVar = [
        ("q", "quit", "quit"),
        ("r", "refresh", "refresh"),
        ("f", "filter", "filter feed"),
        ("p", "pause", "pause feed"),
        ("w", "jump", "jump to workstream"),
    ]

    def __init__(self, *, repo: Path, stand: StandId, state_root: Path) -> None:
        super().__init__()
        self.repo = repo
        self.stand = stand
        self.state_root = state_root
        self.projections = Projections(stand=stand)
        self.counters = Counters(counts={})
        self.paused = False
        self.kind_filter = ""
        self._ledger: SqliteLedger | None = None
        self._feed: deque[tuple[str, FeedLine]] = deque(maxlen=FEED_DEPTH)
        self._selected: ConflictId | None = None
        self._feed_dirty = True

    # -- layout ----------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("connecting...", id="headline")
        with TabbedContent(initial="tab-workstreams"):
            with TabPane("workstreams", id="tab-workstreams"):
                yield DataTable(id="workstreams")
            with TabPane("conflicts", id="tab-conflicts"), Horizontal():
                yield DataTable(id="conflict-list")
                with VerticalScroll():
                    yield Static("select a conflict", id="detail")
            with TabPane("train", id="tab-train"):
                yield DataTable(id="train")
            with TabPane("leases", id="tab-leases"):
                yield DataTable(id="leases")
            with TabPane("events", id="tab-events"):
                yield Input(placeholder="filter by event kind", id="filter")
                yield RichLog(id="feed", markup=False, wrap=True)
            with TabPane("heat", id="tab-heat"):
                yield DataTable(id="heat")
        yield Footer()

    async def on_mount(self) -> None:
        workstreams = self.query_one("#workstreams", DataTable)
        workstreams.cursor_type = "row"
        workstreams.add_columns("agent", "task", "state", "branch", "worktree", "files", "tokens")
        conflicts = self.query_one("#conflict-list", DataTable)
        conflicts.cursor_type = "row"
        conflicts.add_columns("severity", "source", "between")
        train = self.query_one("#train", DataTable)
        train.add_columns("#", "agent", "stage")
        leases = self.query_one("#leases", DataTable)
        leases.add_columns("holder", "mode", "scope")
        heat = self.query_one("#heat", DataTable)
        heat.add_columns("path", "workstreams")

        self._ledger = await SqliteLedger.open(
            self.stand, self.state_root / self.stand / "ledger.db"
        )
        self.set_interval(1.0, self.action_refresh)
        await self.action_refresh()

    # -- the fold --------------------------------------------------------------------

    async def action_refresh(self) -> None:
        if self._ledger is None:
            return
        for envelope in await self._ledger.read(since=self.projections.cursor):
            self.projections.apply(envelope)
            if envelope.kind in render.COUNTED_KINDS:
                self.counters = self.counters.with_event(envelope.kind)
                continue
            line = render.feed_line(envelope)
            if line is not None:
                self._feed.append((envelope.kind, line))
                self._feed_dirty = True
        self._paint()

    def _paint(self) -> None:
        state = self.projections
        lifecycle = state.lifecycle(
            stand_alive(state.pid, self.state_root / self.stand / "ledger.db")
        )
        self.query_one("#headline", Static).update(self._headline(lifecycle))
        self._paint_workstreams()
        self._paint_conflicts()
        self._paint_train()
        self._paint_leases()
        self._paint_heat()
        self._paint_feed()

    def _headline(self, lifecycle: str) -> RenderableType:
        state = self.projections
        head = render.short(state.integration_head or "?")
        heading = Text()
        heading.append(f"{state.title}  ", style="bold")
        heading.append_text(render.lifecycle_banner(lifecycle))
        heading.append(f"\n{state.integration_branch} @ {head} -- {state.summary()}", style="dim")
        note = render.LIFECYCLE_NOTE.get(lifecycle)
        if note is not None:
            heading.append(f"\n{note}", style="yellow")
        return heading

    def _paint_workstreams(self) -> None:
        table = self.query_one("#workstreams", DataTable)
        table.clear()
        rows = render.progress_rows(
            self.projections,
            present={
                item.workstream_id: item.worktree.path.is_dir()
                for item in self.projections.workstreams.values()
            },
        )
        for row in rows:
            workstream = self.projections.workstreams[row.workstream]
            # A workstream whose worktree is gone is history. Rendering it identically
            # to a working agent is how a dead stand comes to look like a live one.
            style = "dim" if row.terminal or not row.present else ""
            table.add_row(
                Text(row.agent, style=style),
                Text(render.clip(row.title, 40), style=style),
                Text(row.state, style=style or render.state_style(row.state)),
                Text(workstream.worktree.branch, style=style),
                Text("present" if row.present else "removed", style=style),
                Text(f"{row.files} files", style=style),
                Text("-" if row.tokens is None else f"{row.tokens:,}", style=style),
                key=str(row.workstream),
            )

    def _paint_conflicts(self) -> None:
        table = self.query_one("#conflict-list", DataTable)
        table.clear()
        for report in self.projections.conflicts.values():
            table.add_row(
                Text(report.severity.label, style=render.severity_style(report.severity)),
                report.source.value,
                " vs ".join(report.between),
                key=str(report.conflict_id),
            )
        self.query_one("#detail", Static).update(self._detail())

    def _detail(self) -> RenderableType:
        """Evidence, hunks and transcript for the selected conflict.

        Until this pane existed the only way to see any of it was
        ``lj conflicts --explain``, which meant leaving the view of the run to find out
        why the run had stopped moving.
        """
        if not self.projections.conflicts:
            return Text("no open conflicts", style="dim")
        selected = self._selected
        if selected is None or selected not in self.projections.conflicts:
            selected = next(iter(self.projections.conflicts))
        report = self.projections.conflicts[selected]
        return render.conflict_detail(report, self.projections.channels.values())

    def _paint_train(self) -> None:
        table = self.query_one("#train", DataTable)
        table.clear()
        for entry in render.train_entries(self.projections):
            style = "bold" if entry.position == 1 else ""
            table.add_row(
                Text(str(entry.position), style=style),
                Text(entry.agent, style=style),
                Text(entry.stage, style="bold red" if "failed" in entry.stage else style),
                key=str(entry.workstream),
            )

    def _paint_leases(self) -> None:
        table = self.query_one("#leases", DataTable)
        table.clear()
        for lease in self.projections.active_leases(self.projections_now()):
            table.add_row(
                str(lease.holder), lease.mode.value, render.clip(lease.scope.describe(), 60)
            )

    def _paint_heat(self) -> None:
        table = self.query_one("#heat", DataTable)
        table.clear()
        for path, count in render.heat_map(self.projections):
            style = "bold red" if count > 2 else ("yellow" if count > 1 else "")
            table.add_row(Text(path, style=style), Text(str(count), style=style))

    def _paint_feed(self) -> None:
        if self.paused or not self._feed_dirty:
            return
        log = self.query_one("#feed", RichLog)
        log.clear()
        for line in self.visible_feed():
            body = Text(f"{line.at:%H:%M:%S}  ", style="dim")
            body.append(line.text, style=line.style)
            log.write(body)
        log.write(render.counters_line(self.counters))
        self._feed_dirty = False

    def visible_feed(self) -> tuple[FeedLine, ...]:
        """The feed after the kind filter.  Public because it is what a test asserts on."""
        return tuple(
            line for kind, line in self._feed if not self.kind_filter or self.kind_filter in kind
        )

    # -- keys ------------------------------------------------------------------------

    def action_filter(self) -> None:
        field = self.query_one("#filter", Input)
        field.add_class("showing")
        self.query_one(TabbedContent).active = "tab-events"
        field.focus()

    def action_pause(self) -> None:
        self.paused = not self.paused
        self._feed_dirty = True
        if not self.paused:
            self._paint_feed()

    def action_jump(self) -> None:
        """Move to the workstream behind whatever is selected.

        A conflict names two workstreams and a train entry names one; both are reasons
        to go and look at what that agent is actually doing.
        """
        target = self._selected_workstream()
        if target is None:
            return
        tabs = self.query_one(TabbedContent)
        tabs.active = "tab-workstreams"
        table = self.query_one("#workstreams", DataTable)
        for index, key in enumerate(table.rows):
            if key.value == str(target):
                table.move_cursor(row=index)
                table.focus()
                return

    def _selected_workstream(self) -> WorkstreamId | None:
        if self._selected is not None and self._selected in self.projections.conflicts:
            return self.projections.conflicts[self._selected].between[0]
        train = self.projections.train
        return train[0] if train else None

    @on(DataTable.RowHighlighted, "#conflict-list")
    def _select_conflict(self, event: DataTable.RowHighlighted) -> None:
        key = event.row_key.value
        if key is not None:
            self._selected = ConflictId(key)
            self.query_one("#detail", Static).update(self._detail())

    @on(Input.Changed, "#filter")
    def _filter_changed(self, event: Input.Changed) -> None:
        self.kind_filter = event.value.strip()
        self._feed_dirty = True
        self._paint_feed()

    @on(Input.Submitted, "#filter")
    def _filter_submitted(self, _event: Input.Submitted) -> None:
        self.query_one("#feed", RichLog).focus()

    def projections_now(self) -> datetime:
        return SystemClock().now()

    async def on_unmount(self) -> None:
        if self._ledger is not None:
            await self._ledger.close()


def run_dashboard(*, repo: Path, stand: StandId, state_root: Path) -> None:
    Dashboard(repo=repo, stand=stand, state_root=state_root).run()
