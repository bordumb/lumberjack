"""``lj watch``, against a replayed ledger.

A smoke test on purpose: that it mounts, folds a real ledger and puts the right state in
each pane.  Textual snapshot testing is not worth the maintenance here -- it fails on
every layout tweak and tells you nothing about whether the operator learned anything.
"""

from __future__ import annotations

from textual.widgets import DataTable

from lumberjack.domain.events import ComponentFailed
from lumberjack.ids import ConflictId, WorkstreamId
from lumberjack.tui.dashboard import Dashboard


def dashboard(recorded_stand) -> Dashboard:
    repo, stand = recorded_stand
    return Dashboard(repo=repo, stand=stand, state_root=repo / ".lumberjack")


async def test_it_mounts_and_folds_the_ledger(recorded_stand):
    app = dashboard(recorded_stand)

    async with app.run_test() as pilot:
        await pilot.pause()

        assert len(app.projections.workstreams) == 2
        assert app.projections.conflicts, "the recorded stand has an open conflict"
        assert app.counters.counts["worktree_delta"] == 2, "counted, never fed"


async def test_the_conflict_pane_shows_evidence_and_the_transcript(recorded_stand):
    """Previously only reachable through ``lj conflicts --explain``."""
    app = dashboard(recorded_stand)

    async with app.run_test() as pilot:
        await pilot.pause()
        app._selected = ConflictId("cfl-recorded")
        detail = app._detail()

    body = _text(detail)
    assert "def alpha() -> str:" in body
    assert "I will take the signature" in body


async def test_the_feed_can_be_filtered_and_paused(recorded_stand):
    app = dashboard(recorded_stand)

    async with app.run_test() as pilot:
        await pilot.pause()
        everything = len(app.visible_feed())

        app.kind_filter = "conflict"
        filtered = app.visible_feed()

        app.action_pause()
        paused = app.paused

    assert everything > len(filtered) > 0
    assert paused is True


async def test_jumping_moves_to_a_workstream_in_the_conflict(recorded_stand):
    app = dashboard(recorded_stand)

    async with app.run_test() as pilot:
        await pilot.pause()
        app._selected = ConflictId("cfl-recorded")

        app.action_jump()
        await pilot.pause()

        assert app.query_one("#workstreams", DataTable).cursor_row == 0
        assert app._selected_workstream() == WorkstreamId("ws-task-render")


async def test_a_stopped_component_reaches_the_headline(recorded_stand):
    """Every pane below a dead oracle is stale, so the warning cannot live in a tab."""
    app = dashboard(recorded_stand)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.projections.degraded["oracle"] = ComponentFailed(
            component="oracle", error="merge-tree exploded", consecutive=4, giving_up=True
        )

        headline = _text(app._headline("live"))

    assert "degraded: oracle stopped" in headline


async def test_the_train_and_the_heat_map_have_rows(recorded_stand):
    app = dashboard(recorded_stand)

    async with app.run_test() as pilot:
        await pilot.pause()

        assert app.query_one("#train", DataTable).row_count == 1, "one workstream asked to land"
        assert app.query_one("#heat", DataTable).row_count > 0, "two workstreams share pkg/core.py"


def _text(renderable) -> str:
    from io import StringIO

    from rich.console import Console

    buffer = StringIO()
    Console(file=buffer, width=120, force_terminal=False).print(renderable)
    return buffer.getvalue()
