"""The event log: one discriminated union, one envelope, total order by ``Seq``.

Everything that happens in a stand is an event.  All state is a projection over these
events, which is what makes ``lj replay`` and deterministic regression fixtures possible.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from lumberjack.domain.accord import Accord, Directive, NegotiationMove, Resolution
from lumberjack.domain.claim import Claim, DenialReason, Lease
from lumberjack.domain.conflict import ConflictReport, Severity
from lumberjack.domain.contract import AmendmentProposal, Contract
from lumberjack.domain.gate import GateReport
from lumberjack.domain.message import Message
from lumberjack.domain.note import Note, ReviewComment
from lumberjack.domain.symbols import SymbolRef
from lumberjack.domain.task import Task, TaskSpec
from lumberjack.domain.workstream import StandConfig, Workstream
from lumberjack.ids import (
    AccordId,
    AgentId,
    ChannelId,
    CommentId,
    CommitSha,
    ConflictId,
    LeaseId,
    ProposalId,
    RepoPath,
    Seq,
    StandId,
    TaskId,
    WorkstreamId,
)

__all__ = [
    "AmendmentAccepted",
    "AmendmentProposed",
    "Bounced",
    "ChannelClosed",
    "ChannelOpened",
    "ClaimRequested",
    "ConflictCleared",
    "ConflictDetected",
    "ContractFrozen",
    "DirectiveIssued",
    "Envelope",
    "EventPayload",
    "GateRun",
    "LandRequested",
    "LeaseDeniedEvent",
    "LeaseExpired",
    "LeaseGrantedEvent",
    "LeaseQueuedEvent",
    "LeaseReleased",
    "MessageSent",
    "NegotiationTurn",
    "NotePosted",
    "ProtocolViolation",
    "ReviewCommentPosted",
    "ReviewCommentResolved",
    "StandHalted",
    "StandStarted",
    "TaskAssigned",
    "TaskPlanned",
    "TaskStateChanged",
    "ViolationCode",
    "ViolationKind",
    "WorkstreamLanded",
    "WorktreeDelta",
    "event_kinds",
]


class _Event(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# -- stand lifecycle -----------------------------------------------------------------


class StandStarted(_Event):
    kind: Literal["stand_started"] = "stand_started"
    stand: StandId
    goal: str
    base: CommitSha
    integration_branch: str
    config: StandConfig


class StandHalted(_Event):
    kind: Literal["stand_halted"] = "stand_halted"
    reason: str
    preserved: tuple[WorkstreamId, ...] = ()


# -- planning and tasks --------------------------------------------------------------


class TaskPlanned(_Event):
    kind: Literal["task_planned"] = "task_planned"
    spec: TaskSpec


class TaskAssigned(_Event):
    kind: Literal["task_assigned"] = "task_assigned"
    task: TaskId
    workstream: Workstream


class TaskStateChanged(_Event):
    kind: Literal["task_state_changed"] = "task_state_changed"
    task_id: TaskId
    frm: str
    to: str
    state: Task
    detail: str = ""


# -- the intent plane ----------------------------------------------------------------


class ClaimRequested(_Event):
    kind: Literal["claim_requested"] = "claim_requested"
    claim: Claim


class LeaseGrantedEvent(_Event):
    kind: Literal["lease_granted"] = "lease_granted"
    lease: Lease
    coexisting: tuple[AgentId, ...] = ()


class LeaseQueuedEvent(_Event):
    kind: Literal["lease_queued"] = "lease_queued"
    claim: Claim
    position: int
    blockers: tuple[AgentId, ...] = ()


class LeaseDeniedEvent(_Event):
    kind: Literal["lease_denied"] = "lease_denied"
    claim: Claim
    reason: DenialReason
    holder: AgentId | None = None


class LeaseReleased(_Event):
    kind: Literal["lease_released"] = "lease_released"
    lease_id: LeaseId
    holder: AgentId


class LeaseExpired(_Event):
    kind: Literal["lease_expired"] = "lease_expired"
    lease_id: LeaseId
    holder: AgentId


# -- the observation plane -----------------------------------------------------------


class WorktreeDelta(_Event):
    kind: Literal["worktree_delta"] = "worktree_delta"
    workstream: WorkstreamId
    snapshot: CommitSha
    paths: tuple[RepoPath, ...] = ()
    symbols: tuple[SymbolRef, ...] = ()
    renames: tuple[tuple[RepoPath, RepoPath], ...] = ()
    deletions: tuple[RepoPath, ...] = ()
    lines_changed: int = 0
    dirty: bool = False


ViolationCode = Literal[
    "undeclared_scope",
    "undeclared_structural_change",
    "protected_path",
    "contract_breach",
    "out_of_turn",
]


class ViolationKind(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: ViolationCode
    detail: str = ""


class ProtocolViolation(_Event):
    kind: Literal["protocol_violation"] = "protocol_violation"
    workstream: WorkstreamId
    by: AgentId
    violation: ViolationKind
    severity: Severity = Severity.WARN
    paths: tuple[RepoPath, ...] = ()


# -- the prediction plane ------------------------------------------------------------


class ConflictDetected(_Event):
    kind: Literal["conflict_detected"] = "conflict_detected"
    report: ConflictReport


class ConflictCleared(_Event):
    kind: Literal["conflict_cleared"] = "conflict_cleared"
    conflict_id: ConflictId
    between: tuple[WorkstreamId, WorkstreamId]
    why: str = ""


# -- arbitration ---------------------------------------------------------------------


class ChannelOpened(_Event):
    kind: Literal["channel_opened"] = "channel_opened"
    channel_id: ChannelId
    conflict_id: ConflictId
    participants: frozenset[AgentId]


class NegotiationTurn(_Event):
    kind: Literal["negotiation_turn"] = "negotiation_turn"
    channel_id: ChannelId
    move: NegotiationMove


class ChannelClosed(_Event):
    kind: Literal["channel_closed"] = "channel_closed"
    channel_id: ChannelId
    state: str
    transcript_ref: str | None = None


class AccordSigned(_Event):
    kind: Literal["accord_signed"] = "accord_signed"
    accord: Accord


class DirectiveIssued(_Event):
    kind: Literal["directive_issued"] = "directive_issued"
    directive: Directive


class ResolutionApplied(_Event):
    kind: Literal["resolution_applied"] = "resolution_applied"
    conflict_id: ConflictId
    resolution: Resolution
    source: Literal["accord", "directive"]
    accord_id: AccordId | None = None
    detail: str = ""


# -- collaboration surfaces ----------------------------------------------------------


class NotePosted(_Event):
    kind: Literal["note_posted"] = "note_posted"
    note: Note


class ReviewCommentPosted(_Event):
    kind: Literal["review_comment_posted"] = "review_comment_posted"
    comment: ReviewComment
    notified: tuple[AgentId, ...] = ()


class ReviewCommentResolved(_Event):
    kind: Literal["review_comment_resolved"] = "review_comment_resolved"
    comment_id: CommentId
    by: AgentId


class MessageSent(_Event):
    kind: Literal["message_sent"] = "message_sent"
    message: Message


class MessageRead(_Event):
    kind: Literal["message_read"] = "message_read"
    reader: AgentId
    up_to: Seq


# -- contracts -----------------------------------------------------------------------


class ContractFrozen(_Event):
    kind: Literal["contract_frozen"] = "contract_frozen"
    contract: Contract


class AmendmentProposed(_Event):
    kind: Literal["amendment_proposed"] = "amendment_proposed"
    proposal: AmendmentProposal


class AmendmentAccepted(_Event):
    kind: Literal["amendment_accepted"] = "amendment_accepted"
    proposal_id: ProposalId
    contract: Contract
    accepted_by: AgentId
    follow_up_tasks: tuple[TaskId, ...] = ()


# -- integration ---------------------------------------------------------------------


class LandRequested(_Event):
    kind: Literal["land_requested"] = "land_requested"
    workstream: WorkstreamId
    tip: CommitSha
    position: int


class GateRun(_Event):
    kind: Literal["gate_run"] = "gate_run"
    workstream: WorkstreamId
    report: GateReport


class WorkstreamLanded(_Event):
    kind: Literal["workstream_landed"] = "workstream_landed"
    workstream: WorkstreamId
    task: TaskId
    merge: CommitSha
    integration_head: CommitSha


class Bounced(_Event):
    kind: Literal["bounced"] = "bounced"
    workstream: WorkstreamId
    task: TaskId
    why: str
    report: GateReport | None = None
    conflict: ConflictReport | None = None
    attempt: int = 1


EventPayload = Annotated[
    StandStarted
    | StandHalted
    | TaskPlanned
    | TaskAssigned
    | TaskStateChanged
    | ClaimRequested
    | LeaseGrantedEvent
    | LeaseQueuedEvent
    | LeaseDeniedEvent
    | LeaseReleased
    | LeaseExpired
    | WorktreeDelta
    | ProtocolViolation
    | ConflictDetected
    | ConflictCleared
    | ChannelOpened
    | NegotiationTurn
    | ChannelClosed
    | AccordSigned
    | DirectiveIssued
    | ResolutionApplied
    | NotePosted
    | ReviewCommentPosted
    | ReviewCommentResolved
    | MessageSent
    | MessageRead
    | ContractFrozen
    | AmendmentProposed
    | AmendmentAccepted
    | LandRequested
    | GateRun
    | WorkstreamLanded
    | Bounced,
    Field(discriminator="kind"),
]

Actor = AgentId | Literal["system"]


class Envelope[E](BaseModel):
    """A ledger record.  Generic so ``Envelope[ConflictDetected]`` is expressible."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: Seq
    at: datetime
    stand: StandId
    actor: Actor
    payload: E

    @property
    def kind(self) -> str:
        kind = getattr(self.payload, "kind", None)
        return str(kind) if kind is not None else "unknown"


def event_kinds() -> frozenset[str]:
    """Every discriminator value in :data:`EventPayload`, for ledger subscriptions."""
    from typing import get_args

    union, _ = get_args(EventPayload)
    return frozenset(member.model_fields["kind"].default for member in get_args(union))
