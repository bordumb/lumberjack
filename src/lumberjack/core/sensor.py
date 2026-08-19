"""The observation plane.

One sensor per workstream turns "what is on disk" into events.  It answers the
question the intent plane cannot: not what an agent *said* it would touch, but what it
actually touched.  Three derived checks fire on every delta, and between them they
catch the failure modes that pure textual conflict detection misses:

* **undeclared scope** -- work outside the held lease (a warning, and an auto-filed
  expanding claim, rather than a halt: agents discover scope as they go);
* **undeclared structural change** -- a rename or delete without an ``EXCLUSIVE``
  lease, which is the one case line-based merging cannot rescue;
* **blast radius** and **contract breach** -- agent A changed something agent B
  depends on, caught before the merge rather than after it.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field

from lumberjack.core.broker import LeaseBroker
from lumberjack.core.projections import Projections
from lumberjack.domain.claim import AccessMode, Claim, PathScope, scopes_overlap
from lumberjack.domain.conflict import (
    ConflictedFile,
    ConflictReport,
    ConflictSource,
    Severity,
)
from lumberjack.domain.events import (
    ConflictDetected,
    ProtocolViolation,
    ViolationCode,
    ViolationKind,
    WorktreeDelta,
)
from lumberjack.domain.glob import matches
from lumberjack.domain.symbols import SymbolRef
from lumberjack.domain.vcs import ChangeKind
from lumberjack.domain.workstream import StandConfig, Workstream
from lumberjack.ids import RepoPath, WorkstreamId, glob_pattern, new_conflict_id
from lumberjack.ports.clock import Clock
from lumberjack.ports.git import GitBackend
from lumberjack.ports.indexer import SymbolIndexer
from lumberjack.ports.ledger import Ledger

__all__ = ["WorktreeSensor"]


@dataclass(slots=True)
class WorktreeSensor:
    workstream: Workstream
    git: GitBackend
    ledger: Ledger
    projections: Projections
    indexer: SymbolIndexer
    clock: Clock
    config: StandConfig
    broker: LeaseBroker | None = None
    _last_paths: frozenset[RepoPath] = field(default_factory=frozenset)
    _task: asyncio.Task[None] | None = None

    @property
    def workstream_id(self) -> WorkstreamId:
        return self.workstream.workstream_id

    async def scan(self) -> WorktreeDelta | None:
        """Compute the delta, publish it, and run the derived checks."""
        snapshot = await self.git.snapshot(self.workstream.worktree)
        # Measure against where the run began, not where this worktree was checked out.
        # A lane picked up in a later session starts from the branch tip, so diffing
        # from the checkout would report a task that has changed 25 files as changing
        # none of them.
        base = self.projections.base or self.workstream.worktree.base
        changes = await self.git.changes(base, snapshot.commit)
        if not changes:
            return None

        symbols: list[SymbolRef] = []
        for change in changes:
            if change.kind is ChangeKind.DELETED:
                continue
            blob = await self.git.read_blob(snapshot.commit, change.path)
            if blob is not None:
                symbols.extend(await self.indexer.symbols_in(change.path, blob))

        delta = WorktreeDelta(
            workstream=self.workstream_id,
            snapshot=snapshot.commit,
            paths=tuple(change.path for change in changes),
            symbols=tuple(symbols),
            renames=tuple(
                (change.old_path, change.path)
                for change in changes
                if change.kind is ChangeKind.RENAMED and change.old_path is not None
            ),
            deletions=tuple(change.path for change in changes if change.kind is ChangeKind.DELETED),
            lines_changed=sum(change.lines_changed for change in changes),
            dirty=snapshot.dirty,
        )
        await self.ledger.append(delta, actor=self.workstream.agent)
        self.projections.deltas[self.workstream_id] = delta

        structural = tuple(change for change in changes if change.kind.is_structural)
        await self._check_protected(delta)
        await self._check_declared_scope(delta)
        await self._check_structural(delta, structural)
        await self._check_blast_radius(delta)
        await self._check_contracts(delta)
        self._last_paths = frozenset(delta.paths)
        return delta

    # -- derived checks --------------------------------------------------------------

    async def _check_protected(self, delta: WorktreeDelta) -> None:
        hits = tuple(
            path
            for path in delta.paths
            for guard in self.config.protected_paths
            if matches(guard, path)
        )
        if hits:
            await self._violation("protected_path", hits, Severity.BLOCK, "harness-managed path")

    async def _check_declared_scope(self, delta: WorktreeDelta) -> None:
        held = self.projections.leases_of(self.workstream_id, self.clock.now())
        undeclared = tuple(
            path
            for path in delta.paths
            if not any(lease.scope.matches_path(path) for lease in held)
        )
        if not undeclared:
            return
        await self._violation(
            "undeclared_scope",
            undeclared,
            Severity.WARN,
            "touched outside any held lease; an expanding claim was filed automatically",
        )
        if self.broker is not None:
            await self.broker.request(
                Claim(
                    claimant=self.workstream.agent,
                    workstream=self.workstream_id,
                    task=self.workstream.task,
                    scope=PathScope(patterns=tuple(glob_pattern(path) for path in undeclared)),
                    mode=AccessMode.EDIT,
                    rationale="auto-filed by the sensor for observed but undeclared edits",
                )
            )

    async def _check_structural(self, delta: WorktreeDelta, structural: tuple[object, ...]) -> None:
        if not structural:
            return
        held = self.projections.leases_of(self.workstream_id, self.clock.now())
        paths = (*(new for _, new in delta.renames), *delta.deletions)
        uncovered = tuple(
            path
            for path in paths
            if not any(
                lease.mode is AccessMode.EXCLUSIVE and lease.scope.matches_path(path)
                for lease in held
            )
        )
        if uncovered:
            await self._violation(
                "undeclared_structural_change",
                uncovered,
                Severity.BLOCK,
                "renames and deletions defeat line-based merging and require an EXCLUSIVE lease",
            )

    async def _check_blast_radius(self, delta: WorktreeDelta) -> None:
        if not delta.symbols or not self.projections.repo_map.modules:
            return
        dependents: set[SymbolRef] = set()
        for symbol in delta.symbols:
            dependents |= await self.indexer.dependents_of(
                symbol, self.projections.repo_map, depth=2
            )
        if not dependents:
            return
        affected = {symbol.path for symbol in dependents}
        now = self.clock.now()
        for other in self.projections.active_workstreams():
            if other.workstream_id == self.workstream_id:
                continue
            other_paths = set(self.projections.observed_paths(other.workstream_id))
            other_scopes = [
                lease.scope for lease in self.projections.leases_of(other.workstream_id, now)
            ]
            hit = affected & other_paths
            declared = any(
                any(scope.matches_path(path) for path in affected) for scope in other_scopes
            )
            if not hit and not declared:
                continue
            await self._conflict(
                other.workstream_id,
                source=ConflictSource.BLAST_RADIUS,
                severity=Severity.WARN if hit else Severity.NOTICE,
                files=tuple(
                    ConflictedFile(
                        path=path,
                        symbols=tuple(s for s in dependents if s.path == path),
                        detail="depends on a symbol this workstream changed",
                    )
                    for path in sorted(hit or affected)[:10]
                ),
                evidence="changed: " + ", ".join(str(symbol) for symbol in delta.symbols[:8]),
            )

    async def _check_contracts(self, delta: WorktreeDelta) -> None:
        for contract in self.projections.contracts_for(self.workstream.task):
            if contract.provider != self.workstream.task:
                continue
            if not contract.breached_by(delta.symbols):
                continue
            touched = tuple(symbol for symbol in delta.symbols if contract.covers(symbol))
            await self._violation(
                "contract_breach",
                tuple(symbol.path for symbol in touched),
                Severity.BLOCK,
                f"contract {contract.contract_id} is frozen; file an amendment first",
            )
            for consumer in contract.consumers:
                other = next(
                    (
                        item.workstream_id
                        for item in self.projections.active_workstreams()
                        if item.task == consumer
                    ),
                    None,
                )
                if other is None:
                    continue
                await self._conflict(
                    other,
                    source=ConflictSource.CONTRACT_BREACH,
                    severity=Severity.BLOCK,
                    files=tuple(
                        ConflictedFile(path=symbol.path, symbols=(symbol,)) for symbol in touched
                    ),
                    evidence=f"frozen surface of {contract.contract_id} changed shape",
                )

    # -- emission --------------------------------------------------------------------

    async def _violation(
        self,
        code: ViolationCode,
        paths: tuple[RepoPath, ...],
        severity: Severity,
        detail: str,
    ) -> None:
        await self.ledger.append(
            ProtocolViolation(
                workstream=self.workstream_id,
                by=self.workstream.agent,
                violation=ViolationKind(code=code, detail=detail),
                severity=severity,
                paths=paths,
            ),
            actor=self.workstream.agent,
        )

    async def _conflict(
        self,
        other: WorkstreamId,
        *,
        source: ConflictSource,
        severity: Severity,
        files: tuple[ConflictedFile, ...],
        evidence: str,
    ) -> None:
        existing = next(
            (
                report
                for report in self.projections.conflicts.values()
                if report.source is source
                and report.pair_key() == tuple(sorted((self.workstream_id, other)))
            ),
            None,
        )
        if existing is not None:
            return
        await self.ledger.append(
            ConflictDetected(
                report=ConflictReport(
                    conflict_id=new_conflict_id(),
                    between=(self.workstream_id, other),
                    source=source,
                    severity=severity,
                    files=files,
                    detected_at=self.clock.now(),
                    evidence=evidence[:4000],
                )
            ),
            actor=self.workstream.agent,
        )

    # -- claim-overlap prior ---------------------------------------------------------

    async def announce_overlaps(self) -> None:
        """Emit the cheap intent-plane prior: declared scopes that overlap."""
        now = self.clock.now()
        mine = self.projections.leases_of(self.workstream_id, now)
        for other in self.projections.active_workstreams():
            if other.workstream_id == self.workstream_id:
                continue
            theirs = self.projections.leases_of(other.workstream_id, now)
            overlapping = [(a, b) for a in mine for b in theirs if scopes_overlap(a.scope, b.scope)]
            if not overlapping:
                continue
            await self._conflict(
                other.workstream_id,
                source=ConflictSource.CLAIM_OVERLAP,
                severity=Severity.NOTICE,
                files=(),
                evidence="; ".join(
                    f"{a.scope.describe()} ({a.mode.value}) vs "
                    f"{b.scope.describe()} ({b.mode.value})"
                    for a, b in overlapping[:5]
                ),
            )

    # -- watching --------------------------------------------------------------------

    async def watch(self, stop: asyncio.Event) -> None:
        """Debounced filesystem watch.  Falls back to polling if watchfiles is absent."""
        try:
            from watchfiles import awatch
        except ImportError:  # pragma: no cover - watchfiles is a hard dependency
            await self._poll(stop)
            return

        debounce = int(self.config.sensor_debounce.total_seconds() * 1000)
        async for _ in awatch(
            self.workstream.worktree.path,
            stop_event=stop,
            debounce=max(50, debounce),
            ignore_permission_denied=True,
        ):
            with contextlib.suppress(Exception):
                await self.scan()

    async def _poll(self, stop: asyncio.Event) -> None:  # pragma: no cover
        while not stop.is_set():
            with contextlib.suppress(Exception):
                await self.scan()
            await self.clock.sleep(self.config.sensor_debounce)
