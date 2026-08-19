"""Protocols describing every I/O boundary.  Nothing in :mod:`lumberjack.core`
imports an adapter; it depends only on these."""

from lumberjack.ports.arbitration import ArbitrationContext, ArbitrationPolicy
from lumberjack.ports.bus import Blackboard, MessageBus
from lumberjack.ports.clock import Clock
from lumberjack.ports.gate import Gate
from lumberjack.ports.git import GitBackend, GitError
from lumberjack.ports.indexer import SymbolIndexer
from lumberjack.ports.ledger import Ledger
from lumberjack.ports.telemetry import NullTelemetry, Span, Telemetry

__all__ = [
    "ArbitrationContext",
    "ArbitrationPolicy",
    "Blackboard",
    "Clock",
    "Gate",
    "GitBackend",
    "GitError",
    "Ledger",
    "MessageBus",
    "NullTelemetry",
    "Span",
    "SymbolIndexer",
    "Telemetry",
]
