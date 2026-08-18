"""Conflict reports -- the output of all three awareness planes."""

from __future__ import annotations

from datetime import datetime
from enum import IntEnum, StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lumberjack.domain.symbols import SymbolRef
from lumberjack.ids import ArtifactRef, ConflictId, RepoPath, WorkstreamId

__all__ = [
    "ConflictReport",
    "ConflictSource",
    "ConflictedFile",
    "Severity",
    "Stage",
]


class ConflictSource(StrEnum):
    MERGE_TREE = "merge_tree"
    """git performed the merge and it conflicted.  Ground truth."""

    CLAIM_OVERLAP = "claim_overlap"
    """Declared intents overlap.  A prior, downgraded when the oracle disagrees."""

    SYMBOL_OVERLAP = "symbol_overlap"
    """Observed deltas touched the same symbol."""

    BLAST_RADIUS = "blast_radius"
    """One workstream changed a symbol another transitively depends on."""

    CONTRACT_BREACH = "contract_breach"
    """A frozen interface changed shape."""


class Severity(IntEnum):
    """Ordered so that ``severity >= Severity.WARN`` reads naturally."""

    NOTICE = 10
    WARN = 20
    BLOCK = 30

    @property
    def label(self) -> str:
        return self.name.lower()


class Stage(StrEnum):
    """Which side of a conflicted file changed."""

    BOTH = "both"
    OURS = "ours"
    THEIRS = "theirs"


class ConflictedFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: RepoPath
    stage: Stage = Stage.BOTH
    hunks: int = Field(default=0, ge=0)
    symbols: tuple[SymbolRef, ...] = ()
    detail: str = ""


class ConflictReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    conflict_id: ConflictId
    between: tuple[WorkstreamId, WorkstreamId]
    source: ConflictSource
    severity: Severity
    files: tuple[ConflictedFile, ...] = ()
    detected_at: datetime
    evidence: str = Field(default="", max_length=4000)
    evidence_ref: ArtifactRef | None = None

    @model_validator(mode="after")
    def _distinct_participants(self) -> Self:
        if self.between[0] == self.between[1]:
            msg = "a workstream cannot conflict with itself"
            raise ValueError(msg)
        return self

    @property
    def participants(self) -> frozenset[WorkstreamId]:
        return frozenset(self.between)

    @property
    def paths(self) -> frozenset[RepoPath]:
        return frozenset(item.path for item in self.files)

    def involves(self, workstream: WorkstreamId) -> bool:
        return workstream in self.between

    def other(self, workstream: WorkstreamId) -> WorkstreamId:
        left, right = self.between
        if workstream == left:
            return right
        if workstream == right:
            return left
        msg = f"{workstream} is not a participant in {self.conflict_id}"
        raise KeyError(msg)

    def pair_key(self) -> tuple[WorkstreamId, WorkstreamId]:
        """Order-independent identity for the pair, so A-vs-B and B-vs-A dedupe."""
        left, right = self.between
        return (left, right) if left <= right else (right, left)

    def downgraded_to(self, severity: Severity) -> ConflictReport:
        return self.model_copy(update={"severity": severity})

    def summary(self) -> str:
        left, right = self.between
        where = ", ".join(sorted(self.paths)[:4]) or "(no files)"
        return f"[{self.severity.label}] {self.source.value}: {left} vs {right} in {where}"
