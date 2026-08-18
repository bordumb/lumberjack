"""Lumberjack -- a typed harness for a swarm of AI agents in parallel git worktrees.

Worktrees solve isolation.  They do nothing for coordination.  This is the
coordination layer: claims that predict, an oracle that decides with real git merges,
and arbitration in which "the manager rules" and "the peers negotiate" are two
implementations of one interface.
"""

from lumberjack.core.supervisor import StandOutcome, Supervisor, WorkstreamOutcome
from lumberjack.domain.workstream import ArbitrationMode, Budget, StandConfig
from lumberjack.stand import Stand

__version__ = "0.1.0"

__all__ = [
    "ArbitrationMode",
    "Budget",
    "Stand",
    "StandConfig",
    "StandOutcome",
    "Supervisor",
    "WorkstreamOutcome",
    "__version__",
]
