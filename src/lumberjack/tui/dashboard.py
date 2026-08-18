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
        workstreams.add_columns("agent", "task", "state", "branch", "drift", "touched")
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
        self.query_one("#headline", Static).update(
            f"[b]{state.goal or self.stand}[/b]\n"
            f"{state.integration_branch} @ {head} -- {state.summary()}"
            + ("  [bold red]HALTED[/bold red]" if state.halted else "")
        )

        table = self.query_one("#workstreams", DataTable)
        table.clear()
        for item in state.workstreams.values():
            task = state.tasks.get(item.task)
            touched = len(state.observed_paths(item.workstream_id))
            table.add_row(
                str(item.agent),
                task.spec.title[:40] if task is not None else str(item.task),
                task.kind if task is not None else "?",
                item.worktree.branch,
                f"{item.drift.behind}" if item.drift.behind else "-",
                f"{touched} files",
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
