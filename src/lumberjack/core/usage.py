"""The running total of what a stand has spent.

One place, so that a budget check and a dashboard read the same number.  Everything
that spends goes through :meth:`UsageLedger.record`, which both accumulates and hands
the same figures to :class:`~lumberjack.ports.telemetry.Telemetry` -- calling the port
directly would keep the export and skip the accounting, which is the wrong half.

This module counts.  It never stops anything: enforcing ``Budget`` is 0004's, and a
component that both measures and halts is one that cannot be tested doing either.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

from lumberjack.domain.usage import UsageTotals
from lumberjack.ids import WorkstreamId
from lumberjack.ports.telemetry import NullTelemetry, Telemetry

if TYPE_CHECKING:
    from pydantic_ai.usage import RunUsage

__all__ = ["FOREMAN_USAGE_KEY", "UsageLedger"]

FOREMAN_USAGE_KEY = WorkstreamId("foreman")
"""Planning and arbitration are spent on the stand, not on any one workstream."""


@dataclass(slots=True)
class UsageLedger:
    """In-memory, additive, cheap to query.

    Deliberately not persisted.  A model call is the most frequent thing that happens in
    a stand, and a write per call would make the accounting cost more than the thing it
    accounts for.  A crashed stand loses its totals; its ledger still has the events.
    """

    telemetry: Telemetry = field(default_factory=NullTelemetry)
    by_workstream: dict[WorkstreamId, UsageTotals] = field(default_factory=dict)

    def record(
        self,
        workstream: WorkstreamId,
        usage: RunUsage,
        *,
        agent: str | None = None,
        model: str | None = None,
        steps: int | None = None,
        wall_clock: timedelta = timedelta(0),
    ) -> None:
        """Add one agent run's usage to the tally, and export it.

        ``steps`` defaults to the model-request count, which is what PydanticAI charges
        a graph step and therefore what ``Budget.max_steps_per_task`` bounds.
        """
        totals = UsageTotals(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            requests=usage.requests,
            steps=usage.requests if steps is None else steps,
            tool_calls=usage.tool_calls,
            wall_clock=wall_clock,
        )
        self.by_workstream[workstream] = self.for_workstream(workstream) + totals
        self.telemetry.record_usage(workstream, usage, agent=agent, model=model)

    def add(self, workstream: WorkstreamId, totals: UsageTotals) -> None:
        """Fold in totals that did not come from a PydanticAI run -- wall clock, mostly."""
        self.by_workstream[workstream] = self.for_workstream(workstream) + totals

    def for_workstream(self, workstream: WorkstreamId) -> UsageTotals:
        """Never raises: an unknown workstream has spent nothing, which is the truth."""
        return self.by_workstream.get(workstream, UsageTotals())

    def totals(self) -> UsageTotals:
        whole = UsageTotals()
        for part in self.by_workstream.values():
            whole = whole + part
        return whole

    def workstreams(self) -> tuple[WorkstreamId, ...]:
        return tuple(self.by_workstream)
