"""Concrete implementations of the ports."""

from lumberjack.adapters.ast_indexer import AstIndexer
from lumberjack.adapters.clock import FrozenClock, SystemClock
from lumberjack.adapters.git_cli import GitCli
from lumberjack.adapters.memory_ledger import MemoryLedger
from lumberjack.adapters.otel import OtelTelemetry, build_telemetry
from lumberjack.adapters.projecting import ProjectingLedger
from lumberjack.adapters.sqlite_ledger import SqliteLedger
from lumberjack.adapters.traced import TracedGate, TracedGit, instrumented
from lumberjack.adapters.uv_gate import CommandGate, NullGate

__all__ = [
    "AstIndexer",
    "CommandGate",
    "FrozenClock",
    "GitCli",
    "MemoryLedger",
    "NullGate",
    "OtelTelemetry",
    "ProjectingLedger",
    "SqliteLedger",
    "SystemClock",
    "TracedGate",
    "TracedGit",
    "build_telemetry",
    "instrumented",
]
