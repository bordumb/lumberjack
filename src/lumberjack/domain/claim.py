"""Scopes, access modes, claims and leases.

The intent plane of the awareness model.  A claim is a *declaration* that an agent
will touch some part of the tree; a lease is a granted claim.  Neither is ground
truth about conflict -- that is the oracle's job (P1: claims predict, git decides).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lumberjack.domain.glob import matches, may_overlap
from lumberjack.domain.symbols import SymbolRef
from lumberjack.ids import AgentId, GlobPattern, LeaseId, RepoPath, TaskId, WorkstreamId, repo_path

__all__ = [
    "AccessMode",
    "Claim",
    "DenialReason",
    "Lease",
    "LeaseDecision",
    "LeaseDenied",
    "LeaseGranted",
    "LeaseQueued",
    "PathScope",
    "Scope",
    "SymbolScope",
    "modes_conflict",
    "scopes_overlap",
]


class AccessMode(StrEnum):
    """How an agent intends to touch a scope.

    ``EDIT`` deliberately coexists with other ``EDIT``s: two agents editing
    different functions in one file merge cleanly, and the oracle will catch the
    cases where they do not.  ``EXCLUSIVE`` is for structural change -- renames,
    deletions, moves, mass reformatting, code generation -- which defeats
    line-based merging and therefore must serialize.
    """

    READ = "read"
    EDIT = "edit"
    EXCLUSIVE = "exclusive"


class PathScope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["path"] = "path"
    patterns: tuple[GlobPattern, ...] = Field(min_length=1)

    def matches_path(self, path: RepoPath) -> bool:
        return any(matches(pattern, path) for pattern in self.patterns)

    def describe(self) -> str:
        return ", ".join(self.patterns)


class SymbolScope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["symbol"] = "symbol"
    symbols: tuple[SymbolRef, ...] = Field(min_length=1)

    def matches_path(self, path: RepoPath) -> bool:
        return any(symbol.path == path for symbol in self.symbols)

    def describe(self) -> str:
        return ", ".join(str(symbol) for symbol in self.symbols)


Scope = Annotated[PathScope | SymbolScope, Field(discriminator="kind")]


def scope_paths(scope: Scope) -> frozenset[RepoPath]:
    """The concrete paths a scope names, where it names any."""
    match scope:
        case SymbolScope(symbols=symbols):
            return frozenset(symbol.path for symbol in symbols)
        case PathScope(patterns=patterns):
            return frozenset(repo_path(p) for p in patterns if not _is_glob(p))


def _is_glob(pattern: str) -> bool:
    from lumberjack.domain.glob import has_magic

    return any(has_magic(segment) or segment == "**" for segment in pattern.split("/"))


def scope_matches(scope: Scope, path: RepoPath) -> bool:
    return scope.matches_path(path)


def scopes_overlap(left: Scope, right: Scope) -> bool:
    """Conservative overlap test between two scopes.

    Returns ``False`` only when the two scopes provably cannot touch a common path.
    """
    match (left, right):
        case (SymbolScope() as a, SymbolScope() as b):
            keys = {symbol.key for symbol in a.symbols}
            if any(symbol.key in keys for symbol in b.symbols):
                return True
            # Same file, different symbols: still worth flagging as a NOTICE-level
            # neighbourhood, but not a symbol collision.
            paths = {symbol.path for symbol in a.symbols}
            return any(symbol.path in paths for symbol in b.symbols)
        case (PathScope() as a, PathScope() as b):
            return any(may_overlap(one, other) for one in a.patterns for other in b.patterns)
        case (SymbolScope() as sym, PathScope() as pat) | (
            PathScope() as pat,
            SymbolScope() as sym,
        ):
            return any(
                matches(pattern, symbol.path) for pattern in pat.patterns for symbol in sym.symbols
            )
    return True  # pragma: no cover - the match above is exhaustive


def modes_conflict(held: AccessMode, wanted: AccessMode) -> bool:
    """Whether an overlapping scope in mode ``wanted`` must wait for mode ``held``.

    ==========  ====  ====  =========
    held\\wanted read  edit  exclusive
    ==========  ====  ====  =========
    read        no    no    yes
    edit        no    no    yes
    exclusive   yes   yes   yes
    ==========  ====  ====  =========
    """
    return AccessMode.EXCLUSIVE in (held, wanted)


class Claim(BaseModel):
    """A declared intent to touch a scope."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claimant: AgentId
    workstream: WorkstreamId
    task: TaskId
    scope: Scope
    mode: AccessMode
    rationale: str = Field(min_length=1, max_length=500)
    ttl: timedelta = timedelta(minutes=30)

    @model_validator(mode="after")
    def _positive_ttl(self) -> Self:
        if self.ttl <= timedelta(0):
            msg = "claim ttl must be positive"
            raise ValueError(msg)
        return self


class Lease(BaseModel):
    """A granted claim."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lease_id: LeaseId
    claim: Claim
    granted_at: datetime
    expires_at: datetime

    @property
    def holder(self) -> AgentId:
        return self.claim.claimant

    @property
    def workstream(self) -> WorkstreamId:
        return self.claim.workstream

    @property
    def mode(self) -> AccessMode:
        return self.claim.mode

    @property
    def scope(self) -> Scope:
        return self.claim.scope

    def expired_at(self, now: datetime) -> bool:
        return now >= self.expires_at

    def renewed(self, now: datetime, ttl: timedelta | None = None) -> Lease:
        return self.model_copy(update={"expires_at": now + (ttl or self.claim.ttl)})


class DenialReason(StrEnum):
    EXCLUSIVE_HELD = "exclusive_held"
    OUT_OF_TASK_SCOPE = "out_of_task_scope"
    STAND_HALTED = "stand_halted"
    WORKSTREAM_UNKNOWN = "workstream_unknown"
    PROTECTED_PATH = "protected_path"


class LeaseGranted(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["granted"] = "granted"
    lease: Lease
    coexisting: tuple[AgentId, ...] = Field(
        default=(),
        description="Peers holding overlapping non-conflicting leases; notified both ways.",
    )


class LeaseQueued(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["queued"] = "queued"
    claim: Claim
    position: int = Field(ge=1)
    blockers: tuple[AgentId, ...]
    eta: datetime | None = None


class LeaseDenied(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["denied"] = "denied"
    claim: Claim
    reason: DenialReason
    holder: AgentId | None = None
    suggestion: str | None = None


LeaseDecision = Annotated[LeaseGranted | LeaseQueued | LeaseDenied, Field(discriminator="kind")]
