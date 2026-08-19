"""What a run has cost so far.

0002 owns *measuring* -- it builds the concrete ``UsageLedger`` and records against it.
This module is only the shape 0004 enforces ``Budget`` against, written as a Protocol so
that the two specs can land in either order and nothing here duplicates the collection.

The two methods are the ones 0002 froze on the blackboard, and ``steps`` is the number
``Budget.max_steps_per_task`` is compared against.  Neither method raises: an unknown
workstream reports zeros.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import timedelta

    from lumberjack.ids import WorkstreamId

__all__ = ["UsageLedger", "UsageTotals"]


class UsageTotals(Protocol):
    """Structural: satisfied by 0002's frozen ``UsageTotals`` model as written.

    Read-only on purpose.  Enforcement only ever asks what has been spent, and a
    protocol that demanded writable attributes would exclude the frozen model 0002
    actually publishes.
    """

    @property
    def input_tokens(self) -> int: ...

    @property
    def output_tokens(self) -> int: ...

    @property
    def requests(self) -> int: ...

    @property
    def steps(self) -> int: ...

    @property
    def wall_clock(self) -> timedelta: ...

    @property
    def total_tokens(self) -> int: ...


class UsageLedger(Protocol):
    def totals(self) -> UsageTotals:
        """Everything this stand has spent, across every workstream."""
        ...

    def for_workstream(self, workstream: WorkstreamId) -> UsageTotals:
        """One workstream's spend.  Zeros for a workstream that has not run."""
        ...
