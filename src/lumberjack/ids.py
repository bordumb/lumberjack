"""Identifier types.

Every identifier is a distinct :func:`typing.NewType` over ``str`` so that a
``TaskId`` can never be passed where a ``WorkstreamId`` is expected.  Pydantic
understands ``NewType`` natively, so these cost nothing at the schema boundary.
"""

from __future__ import annotations

import re
from typing import Annotated, NewType
from uuid import uuid4

from pydantic import AfterValidator

__all__ = [
    "AccordId",
    "AgentId",
    "ArtifactRef",
    "ChannelId",
    "CommentId",
    "CommitSha",
    "ConflictId",
    "ContractId",
    "GlobPattern",
    "LeaseId",
    "MessageId",
    "NoteId",
    "ProposalId",
    "RepoPath",
    "Seq",
    "StandId",
    "TaskId",
    "TreeSha",
    "WorkstreamId",
    "glob_pattern",
    "new_accord_id",
    "new_agent_id",
    "new_channel_id",
    "new_comment_id",
    "new_conflict_id",
    "new_contract_id",
    "new_lease_id",
    "new_message_id",
    "new_note_id",
    "new_proposal_id",
    "new_stand_id",
    "new_task_id",
    "new_workstream_id",
    "repo_path",
]

StandId = NewType("StandId", str)
AgentId = NewType("AgentId", str)
TaskId = NewType("TaskId", str)
WorkstreamId = NewType("WorkstreamId", str)
LeaseId = NewType("LeaseId", str)
ConflictId = NewType("ConflictId", str)
AccordId = NewType("AccordId", str)
ChannelId = NewType("ChannelId", str)
ContractId = NewType("ContractId", str)
MessageId = NewType("MessageId", str)
NoteId = NewType("NoteId", str)
ProposalId = NewType("ProposalId", str)
ArtifactRef = NewType("ArtifactRef", str)
CommentId = NewType("CommentId", str)

Seq = NewType("Seq", int)
"""Total-order position in the ledger.  Assigned by the ledger, never by callers."""

CommitSha = NewType("CommitSha", str)
TreeSha = NewType("TreeSha", str)


def _short(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def new_stand_id() -> StandId:
    return StandId(_short("stand"))


def new_agent_id(role: str = "agent") -> AgentId:
    return AgentId(_short(role))


def new_task_id() -> TaskId:
    return TaskId(_short("task"))


def new_workstream_id() -> WorkstreamId:
    return WorkstreamId(_short("ws"))


def new_lease_id() -> LeaseId:
    return LeaseId(_short("lease"))


def new_conflict_id() -> ConflictId:
    return ConflictId(_short("cfl"))


def new_accord_id() -> AccordId:
    return AccordId(_short("accord"))


def new_channel_id() -> ChannelId:
    return ChannelId(_short("chan"))


def new_contract_id() -> ContractId:
    return ContractId(_short("contract"))


def new_message_id() -> MessageId:
    return MessageId(_short("msg"))


def new_note_id() -> NoteId:
    return NoteId(_short("note"))


def new_proposal_id() -> ProposalId:
    return ProposalId(_short("prop"))


def new_comment_id() -> CommentId:
    return CommentId(_short("comment"))


_WINDOWS_SEP = re.compile(r"\\+")


def _normalize_repo_path(value: str) -> str:
    """Normalize to a repo-relative POSIX path.

    Rejects absolute paths and any path that escapes the repository root, because
    a claim that escapes the repo is a claim on someone else's machine.
    """
    raw = _WINDOWS_SEP.sub("/", value.strip())
    if not raw:
        msg = "repo path must not be empty"
        raise ValueError(msg)
    if raw.startswith("/") or re.match(r"^[A-Za-z]:/", raw):
        msg = f"repo path must be repository-relative, got {value!r}"
        raise ValueError(msg)

    parts: list[str] = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                msg = f"repo path escapes the repository root: {value!r}"
                raise ValueError(msg)
            parts.pop()
            continue
        parts.append(part)
    if not parts:
        msg = f"repo path resolves to the repository root: {value!r}"
        raise ValueError(msg)
    return "/".join(parts)


RepoPath = Annotated[str, AfterValidator(_normalize_repo_path)]
"""A normalized, repository-relative POSIX path."""


def repo_path(value: str) -> RepoPath:
    """Construct a :data:`RepoPath`, validating it the way pydantic would."""
    return _normalize_repo_path(value)


_GLOB_OK = re.compile(r"^[A-Za-z0-9_\-./*?\[\]{},!+@^]+$")


def _validate_glob(value: str) -> str:
    raw = _WINDOWS_SEP.sub("/", value.strip())
    if not raw:
        msg = "glob pattern must not be empty"
        raise ValueError(msg)
    if raw.startswith("/"):
        msg = f"glob pattern must be repository-relative, got {value!r}"
        raise ValueError(msg)
    if ".." in raw.split("/"):
        msg = f"glob pattern escapes the repository root: {value!r}"
        raise ValueError(msg)
    if not _GLOB_OK.match(raw):
        msg = f"glob pattern contains unsupported characters: {value!r}"
        raise ValueError(msg)
    return raw


GlobPattern = Annotated[str, AfterValidator(_validate_glob)]
"""A validated, repository-relative glob pattern (``pathlib``/``fnmatch`` syntax)."""


def glob_pattern(value: str) -> GlobPattern:
    """Construct a :data:`GlobPattern`, validating it the way pydantic would."""
    return _validate_glob(value)


def _validate_sha(value: str) -> str:
    raw = value.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{7,64}", raw):
        msg = f"not a git object id: {value!r}"
        raise ValueError(msg)
    return raw


Sha = Annotated[str, AfterValidator(_validate_sha)]
"""A validated git object id, used where a model field must hold a real oid."""
