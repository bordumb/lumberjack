"""The live view during ``lj run``.

The ledger is subscribable and every claim, conflict, negotiation turn and landing is
already an event in it.  Before this, none of that reached the screen while it mattered:
``lj run`` printed one line and went silent for minutes, which is not something anyone
watches a swarm edit their repository under twice.

This is a reader like any other -- its own
:class:`~lumberjack.core.projections.Projections`, folded from its own subscription -- so
attaching it cannot perturb the run.
"""

from __future__ import annotations

import contextlib
import logging
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

from rich.console import RenderableType
from rich.live import Live

from lumberjack.cli import render
from lumberjack.cli.output import Output
from lumberjack.cli.render import Counters, FeedLine, UsageByWorkstream
from lumberjack.core.projections import Projections
from lumberjack.domain.events import Envelope, EventPayload
from lumberjack.ids import StandId, WorkstreamId
from lumberjack.ports.ledger import Ledger

__all__ = ["LiveRun", "follow", "worktrees_present"]

log = logging.getLogger(__name__)

FEED_DEPTH = 12
"""How much of the feed a redrawing frame keeps.  A terminal is not a log file."""


@dataclass(slots=True)
class LiveRun:
    """Folds a stand's events into what a person watching one needs to see.

    Everything the screen shows comes out of :mod:`lumberjack.cli.render`, so the same
    state is assertable in a test without capturing a terminal.
    """

    stand: StandId
    output: Output
    projections: Projections
    feed: deque[FeedLine] = field(default_factory=lambda: deque(maxlen=FEED_DEPTH))
    counters: Counters = field(default_factory=lambda: Counters(counts={}))

    @classmethod
    def start(cls, stand: StandId, output: Output) -> LiveRun:
        return cls(stand=stand, output=output, projections=Projections(stand=stand))

    def absorb(self, envelope: Envelope[EventPayload]) -> FeedLine | None:
        """Fold one event.  Returns the feed line it produced, where it produced one."""
        self.projections.apply(envelope)
        if envelope.kind in render.COUNTED_KINDS:
            self.counters = self.counters.with_event(envelope.kind)
            return None
        line = render.feed_line(envelope)
        if line is not None:
            self.feed.append(line)
        return line

    def view(self, *, usage: UsageByWorkstream | None = None) -> RenderableType:
        state = self.projections
        return render.live_view(
            title=state.title,
            lifecycle=state.lifecycle(),
            integration=state.integration_branch or "(no branch yet)",
            head=state.integration_head,
            rows=render.progress_rows(state, usage=usage),
            feed=tuple(self.feed),
            counters=self.counters,
        )


async def follow(
    ledger: Ledger,
    *,
    stand: StandId,
    output: Output,
    usage: UsageByWorkstream | None = None,
) -> LiveRun:
    """Render a stand until the caller cancels this task.

    On a terminal that is a frame updating in place.  Anywhere else -- a pipe, a CI job,
    ``lj run > log.txt`` -- it degrades to one line per event, because a frame redrawn a
    hundred times into a file is unreadable and a log is not.
    """
    live = LiveRun.start(stand, output)
    if output.rich:
        await _follow_rich(ledger, live, usage=usage)
    else:
        await _follow_plain(ledger, live)
    return live


async def _follow_plain(ledger: Ledger, live: LiveRun) -> None:
    def show(line: FeedLine | None) -> None:
        if line is not None:
            live.output.line(render.plain_feed_line(line), style=line.style)

    await _drain(ledger, live, show)


async def _follow_rich(ledger: Ledger, live: LiveRun, *, usage: UsageByWorkstream | None) -> None:
    with Live(
        live.view(usage=usage),
        console=live.output.stdout,
        refresh_per_second=4,
        transient=True,
    ) as frame:
        await _drain(ledger, live, lambda _line: frame.update(live.view(usage=usage)))


async def _drain(
    ledger: Ledger, live: LiveRun, show: Callable[[FeedLine | None], None]
) -> None:
    stream = ledger.subscribe()
    try:
        async for envelope in stream:
            show(live.absorb(envelope))
    finally:
        log.debug("live view for %s stopped at seq %s", live.stand, live.projections.cursor)
        await _aclose(stream)


async def _aclose(stream: AsyncIterator[Envelope[EventPayload]]) -> None:
    """Close the subscription on the loop that ran it.

    A cancelled ``async for`` leaves the generator suspended, and letting the garbage
    collector finalize it produces a warning from an unrelated part of the run. Ledgers
    are free to return a plain iterator, so this asks rather than assumes.
    """
    aclose = getattr(stream, "aclose", None)
    if aclose is None:
        return
    # A generator cancelled at its own await point raises again on close; there is
    # nothing left to clean up at that point, and the run is already over.
    with contextlib.suppress(RuntimeError):
        await aclose()


def worktrees_present(projections: Projections) -> dict[WorkstreamId, bool]:
    """Which workstreams still have a worktree on disk.

    The one filesystem question :mod:`lumberjack.cli.render` is not allowed to ask
    itself, so it is answered out here and passed in.
    """
    return {
        item.workstream_id: item.worktree.path.is_dir()
        for item in projections.workstreams.values()
    }
