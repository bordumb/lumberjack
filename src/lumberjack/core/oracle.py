"""The prediction plane.

``git merge-tree --write-tree`` performs a real merge between two commits with no
worktree and no checkout, in tens of milliseconds.  That makes it cheap enough to run
continuously and authoritative enough to overrule every heuristic in the system:
a declared-overlap warning that the oracle disproves is cleared, not escalated.

Uncommitted work participates because :meth:`GitBackend.snapshot` turns each worktree
into an unreferenced commit first, so agents never have to commit to become visible.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from lumberjack.core.projections import Projections
from lumberjack.domain.conflict import (
    ConflictedFile,
    ConflictReport,
    ConflictSource,
    Severity,
)
from lumberjack.domain.events import ConflictCleared, ConflictDetected
from lumberjack.domain.symbols import SymbolRef
from lumberjack.domain.workstream import Snapshot, StandConfig
from lumberjack.ids import CommitSha, RepoPath, WorkstreamId, new_conflict_id
from lumberjack.ports.clock import Clock
from lumberjack.ports.git import GitBackend
from lumberjack.ports.ledger import Ledger
from lumberjack.ports.telemetry import NullTelemetry, Telemetry

__all__ = ["ConflictOracle", "PairKey"]

PairKey = tuple[WorkstreamId, WorkstreamId]

_ORACLE_SOURCES = frozenset(
    {
        ConflictSource.MERGE_TREE,
        ConflictSource.CLAIM_OVERLAP,
        ConflictSource.SYMBOL_OVERLAP,
    }
)


@dataclass(slots=True)
class ConflictOracle:
    git: GitBackend
    ledger: Ledger
    projections: Projections
    clock: Clock
    config: StandConfig
    telemetry: Telemetry = field(default_factory=NullTelemetry)
    snapshots: dict[WorkstreamId, Snapshot] = field(default_factory=dict)
    open_pairs: dict[PairKey, ConflictReport] = field(default_factory=dict)

    async def snapshot(self, workstream: WorkstreamId) -> Snapshot | None:
        found = self.projections.workstreams.get(workstream)
        if found is None or not found.active:
            return None
        snapshot = await self.git.snapshot(found.worktree)
        self.snapshots[workstream] = snapshot
        return snapshot

    async def probe_all(self) -> tuple[ConflictReport, ...]:
        """Pairwise probe across active workstreams, plus each against integration."""
        active = [item.workstream_id for item in self.projections.active_workstreams()]
        for workstream in active:
            await self.snapshot(workstream)

        reports: list[ConflictReport] = []
        for index, left in enumerate(active):
            for right in active[index + 1 :]:
                report = await self.probe_pair(left, right, refresh=False)
                if report is not None:
                    reports.append(report)
        for workstream in active:
            report = await self.probe_integration(workstream, refresh=False)
            if report is not None:
                reports.append(report)
        return tuple(reports)

    async def probe_pair(
        self, left: WorkstreamId, right: WorkstreamId, *, refresh: bool = True
    ) -> ConflictReport | None:
        """``refresh`` re-snapshots first; ``probe_all`` turns it off, having just done so.

        A cached snapshot is a stale answer, and a stale answer here is worse than none:
        it is a conflict the agents have already resolved, still on their screen.
        """
        key: PairKey = (left, right) if left <= right else (right, left)
        # The unit cost of the O(n^2) sweep, and therefore the only evidence that can
        # settle "optimal N" (0001_SPEC.md §18.2).  Timed from here so the prefilter's
        # saving shows up as the cheap probes it makes, not as probes that never happened.
        started = time.perf_counter()
        left_snapshot = await self._snapshot_for(left, refresh=refresh)
        right_snapshot = await self._snapshot_for(right, refresh=refresh)
        if left_snapshot is None or right_snapshot is None:
            return None  # nothing was probed, so there is nothing to time

        # The cheap prefilter that keeps the O(n^2) sweep affordable: if the two
        # workstreams have not touched a common path, git cannot possibly conflict.
        left_paths = set(self.projections.observed_paths(left)) | set(left_snapshot.paths)
        right_paths = set(self.projections.observed_paths(right)) | set(right_snapshot.paths)
        if left_paths and right_paths and not (left_paths & right_paths):
            await self._clear(key, "no common paths")
            self._probed(started, clean=True, prefiltered=True)
            return None

        base = await self.git.merge_base(left_snapshot.commit, right_snapshot.commit)
        merged = await self.git.merge_tree(left_snapshot.commit, right_snapshot.commit, base=base)
        self._probed(started, clean=merged.clean, prefiltered=False)
        if merged.clean:
            await self._clear(key, "oracle: merge is clean")
            return None
        return await self._raise(
            key,
            source=ConflictSource.MERGE_TREE,
            severity=Severity.BLOCK,
            paths=merged.conflicted,
            evidence=merged.messages,
        )

    def _probed(self, started: float, *, clean: bool, prefiltered: bool) -> None:
        self.telemetry.histogram(
            "lj.oracle.probe_pair",
            (time.perf_counter() - started) * 1000,
            clean=clean,
            prefiltered=prefiltered,
        )

    async def _snapshot_for(self, workstream: WorkstreamId, *, refresh: bool) -> Snapshot | None:
        if refresh:
            return await self.snapshot(workstream)
        return self.snapshots.get(workstream) or await self.snapshot(workstream)

    async def probe_integration(
        self, workstream: WorkstreamId, *, refresh: bool = True
    ) -> ConflictReport | None:
        head = self.projections.integration_head
        snapshot = await self._snapshot_for(workstream, refresh=refresh)
        if head is None or snapshot is None:
            return None
        base = await self.git.merge_base(snapshot.commit, head)
        merged = await self.git.merge_tree(head, snapshot.commit, base=base)
        behind = await self.git.commits_between(snapshot.commit, head)
        self._record_drift(workstream, head, behind, rebase_clean=merged.clean)
        if merged.clean:
            return None
        return await self._raise(
            (workstream, _INTEGRATION),
            source=ConflictSource.MERGE_TREE,
            severity=Severity.WARN,
            paths=merged.conflicted,
            evidence=f"conflicts with integration head {head[:8]}\n{merged.messages}",
        )

    async def would_land_cleanly(
        self, workstream: WorkstreamId
    ) -> tuple[bool, tuple[RepoPath, ...]]:
        """Pre-check for the merge train: never burn a gate run on a doomed merge."""
        head = self.projections.integration_head
        found = self.projections.workstreams.get(workstream)
        if head is None or found is None or found.tip is None:
            return True, ()
        base = await self.git.merge_base(found.tip, head)
        merged = await self.git.merge_tree(head, found.tip, base=base)
        return merged.clean, merged.conflicted

    def _record_drift(
        self,
        workstream: WorkstreamId,
        head: CommitSha,
        behind: int,
        *,
        rebase_clean: bool,
    ) -> None:
        from lumberjack.domain.workstream import DriftStatus

        found = self.projections.workstreams.get(workstream)
        if found is None:
            return
        self.projections.workstreams[workstream] = found.model_copy(
            update={
                "drift": DriftStatus(
                    behind=behind, integration_head=head, rebase_clean=rebase_clean
                )
            }
        )

    async def _raise(
        self,
        key: PairKey,
        *,
        source: ConflictSource,
        severity: Severity,
        paths: tuple[RepoPath, ...],
        evidence: str,
    ) -> ConflictReport:
        previous = self.open_pairs.get(key)
        if (
            previous is not None
            and previous.source is source
            and previous.paths == frozenset(paths)
        ):
            return previous
        report = ConflictReport(
            conflict_id=new_conflict_id(),
            between=key,
            source=source,
            severity=severity,
            files=tuple(
                ConflictedFile(path=path, symbols=self._symbols_at(key, path)) for path in paths
            ),
            detected_at=self.clock.now(),
            evidence=evidence[:4000],
        )
        if previous is not None:
            await self._clear(key, "superseded")
        self.open_pairs[key] = report
        await self.ledger.append(ConflictDetected(report=report))
        # Counted per raise, not per open conflict: whether the oracle earns its keep is
        # a question about how often it finds something, not how much is outstanding.
        self.telemetry.counter(
            "lj.oracle.conflict", source=source.value, severity=severity.label
        )
        return report

    def _symbols_at(self, key: PairKey, path: RepoPath) -> tuple[SymbolRef, ...]:
        found: list[SymbolRef] = []
        for workstream in key:
            found.extend(
                symbol
                for symbol in self.projections.observed_symbols(workstream)
                if symbol.path == path
            )
        return tuple(dict.fromkeys(found))

    async def _clear(self, key: PairKey, why: str) -> None:
        report = self.open_pairs.pop(key, None)
        if report is not None:
            await self.ledger.append(
                ConflictCleared(conflict_id=report.conflict_id, between=key, why=why)
            )
        # P1 in action: a declared-overlap or symbol-overlap warning the oracle has
        # just disproved is cleared rather than left to frighten the agents.
        for open_report in tuple(self.projections.conflicts.values()):
            if (
                open_report.pair_key() == key
                and open_report.source in _ORACLE_SOURCES
                and open_report.source is not ConflictSource.MERGE_TREE
            ):
                await self.ledger.append(
                    ConflictCleared(
                        conflict_id=open_report.conflict_id, between=key, why=f"oracle: {why}"
                    )
                )


_INTEGRATION = WorkstreamId("integration")
