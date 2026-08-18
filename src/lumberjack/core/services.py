"""The service bundle handed to agents and to the supervisor.

One object so that dependency wiring happens once, at the edge, and every consumer
below it is testable by substituting adapters.
"""

from __future__ import annotations

from dataclasses import dataclass

from lumberjack.core.board import LedgerBlackboard, LedgerMessageBus
from lumberjack.core.broker import LeaseBroker
from lumberjack.core.digest import DigestBuilder
from lumberjack.core.oracle import ConflictOracle
from lumberjack.core.projections import Projections
from lumberjack.core.resolve import RulingExecutor
from lumberjack.core.train import MergeTrain
from lumberjack.domain.workstream import StandConfig
from lumberjack.ids import StandId
from lumberjack.ports.clock import Clock
from lumberjack.ports.gate import Gate
from lumberjack.ports.git import GitBackend
from lumberjack.ports.indexer import SymbolIndexer
from lumberjack.ports.ledger import Ledger

__all__ = ["Services"]


@dataclass(frozen=True, slots=True)
class Services:
    stand: StandId
    config: StandConfig
    clock: Clock
    git: GitBackend
    ledger: Ledger
    indexer: SymbolIndexer
    gate: Gate
    projections: Projections
    broker: LeaseBroker
    oracle: ConflictOracle
    board: LedgerBlackboard
    bus: LedgerMessageBus
    digest: DigestBuilder
    train: MergeTrain
    executor: RulingExecutor

    @classmethod
    def wire(
        cls,
        *,
        stand: StandId,
        config: StandConfig,
        clock: Clock,
        git: GitBackend,
        ledger: Ledger,
        indexer: SymbolIndexer,
        gate: Gate,
        projections: Projections | None = None,
    ) -> Services:
        state = projections if projections is not None else Projections(stand=stand)
        broker = LeaseBroker(ledger=ledger, projections=state, clock=clock, config=config)
        oracle = ConflictOracle(
            git=git, ledger=ledger, projections=state, clock=clock, config=config
        )
        board = LedgerBlackboard(ledger=ledger, projections=state, clock=clock)
        bus = LedgerMessageBus(ledger=ledger, projections=state, clock=clock)
        return cls(
            stand=stand,
            config=config,
            clock=clock,
            git=git,
            ledger=ledger,
            indexer=indexer,
            gate=gate,
            projections=state,
            broker=broker,
            oracle=oracle,
            board=board,
            bus=bus,
            digest=DigestBuilder(projections=state, clock=clock, config=config),
            train=MergeTrain(
                git=git,
                ledger=ledger,
                projections=state,
                oracle=oracle,
                gate=gate,
                clock=clock,
                config=config,
            ),
            executor=RulingExecutor(
                ledger=ledger,
                projections=state,
                broker=broker,
                board=board,
                bus=bus,
                clock=clock,
            ),
        )
