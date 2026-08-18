"""``lj watch``: a live view of the stand.

Everything here is a fold over the ledger, so the dashboard is a reader like any
other -- it never talks to the coordinator, and attaching one cannot perturb a run.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, Static

from lumberjack.adapters.clock import SystemClock
from lumberjack.adapters.sqlite_ledger import SqliteLedger
from lumberjack.core.projections import Projections
from lumberjack.ids import StandId

__all__ = ["Dashboard", "run_dashboard"]

_SEVERITY_STYLE = {"notice": "dim", "warn": "yellow", "block": "bold red"}
_TERMINAL = frozenset({"landed", "blocked", "abandoned"})
_BANNER = {
    "halted": "[bold red]HALTED[/bold red] -- stopped; nothing below is running",
    "finished": "[bold yellow]FINISHED[/bold yellow] -- every task is terminal",
    "live": "[bold green]LIVE[/bold green]",
}


class Dashboard(App[None]):
    """Workstream lanes, live claims, conflicts and the merge train."""

    CSS = """
    Screen { layout: vertical; }
    #headline { height: 3; padding: 0 1; }
    DataTable { height: 1fr; }
    .pane { border: round $primary; }
    """
    BINDINGS: ClassVar = [("q", "quit", "quit"), ("r", "refresh", "refresh")]

    def __init__(self, *, repo: Path, stand: StandId, state_root: Path) -> None:
        super().__init__()
        self.repo = repo
        self.stand = stand
        self.state_root = state_root
        self.projections = Projections(stand=stand)
        self._ledger: SqliteLedger | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("connecting...", id="headline")
        with Vertical():
            yield DataTable(id="workstreams", classes="pane")
            with Horizontal():
                yield DataTable(id="conflicts", classes="pane")
                yield DataTable(id="leases", classes="pane")
        yield Footer()

    async def on_mount(self) -> None:
        workstreams = self.query_one("#workstreams", DataTable)
        workstreams.add_columns("agent", "task", "state", "branch", "worktree", "touched")
        conflicts = self.query_one("#conflicts", DataTable)
        conflicts.add_columns("severity", "source", "between", "files")
        leases = self.query_one("#leases", DataTable)
        leases.add_columns("holder", "mode", "scope")

        self._ledger = await SqliteLedger.open(
            self.stand, self.state_root / self.stand / "ledger.db"
        )
        self.set_interval(1.0, self.action_refresh)
        await self.action_refresh()

    async def action_refresh(self) -> None:
        if self._ledger is None:
            return
        await self.projections.hydrate(self._ledger)
        state = self.projections
        head = (state.integration_head or "?")[:8]
        lifecycle = state.lifecycle()
        banner = _BANNER[lifecycle]
        self.query_one("#headline", Static).update(
            f"[b]{state.goal or self.stand}[/b]  {banner}\n"
            f"{state.integration_branch} @ {head} -- {state.summary()}"
        )

        table = self.query_one("#workstreams", DataTable)
        table.clear()
        for item in state.workstreams.values():
            task = state.tasks.get(item.task)
            kind = task.kind if task is not None else "?"
            touched = len(state.observed_paths(item.workstream_id))
            # A workstream whose worktree is gone is history. Rendering it identically
            # to a working agent is how a dead stand comes to look like a live one.
            present = item.worktree.path.is_dir()
            dim = not present or kind in _TERMINAL
            wrap = (lambda text: f"[dim]{text}[/dim]") if dim else (lambda text: text)
            table.add_row(
                wrap(str(item.agent)),
                wrap(task.spec.title[:40] if task is not None else str(item.task)),
                wrap(kind),
                wrap(item.worktree.branch),
                wrap("present" if present else "removed"),
                wrap(f"{touched} files"),
            )

        conflicts = self.query_one("#conflicts", DataTable)
        conflicts.clear()
        for report in state.conflicts.values():
            style = _SEVERITY_STYLE.get(report.severity.label, "")
            label = (
                f"[{style}]{report.severity.label}[/{style}]" if style else report.severity.label
            )
            conflicts.add_row(
                label,
                report.source.value,
                " vs ".join(report.between),
                ", ".join(sorted(report.paths)[:3]) or "-",
            )

        leases = self.query_one("#leases", DataTable)
        leases.clear()
        for lease in state.active_leases(self.projections_now()):
            leases.add_row(str(lease.holder), lease.mode.value, lease.scope.describe()[:60])

    def projections_now(self) -> datetime:
        return SystemClock().now()

    async def on_unmount(self) -> None:
        if self._ledger is not None:
            await self._ledger.close()


def run_dashboard(*, repo: Path, stand: StandId, state_root: Path) -> None:
    Dashboard(repo=repo, stand=stand, state_root=state_root).run()
