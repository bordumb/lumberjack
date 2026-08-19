"""Derived state.

Every query the system makes is answered from a projection over the ledger, never
from a side-channel.  A projection bug is therefore a rebuild, not a data loss, and
``lj replay`` reconstructs any historical view exactly.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import assert_never

from lumberjack.domain.accord import Channel, ChannelState
from lumberjack.domain.claim import Claim, Lease, scopes_overlap
from lumberjack.domain.conflict import ConflictReport, Severity
from lumberjack.domain.contract import AmendmentProposal, Contract
from lumberjack.domain.events import (
    AccordSigned,
    AmendmentAccepted,
    AmendmentProposed,
    Bounced,
    ChannelClosed,
    ChannelOpened,
    ClaimRequested,
    ComponentFailed,
    ConflictCleared,
    ConflictDetected,
    ContractFrozen,
    DirectiveIssued,
    Envelope,
    EventPayload,
    GateRun,
    LandRequested,
    LeaseDeniedEvent,
    LeaseExpired,
    LeaseGrantedEvent,
    LeaseQueuedEvent,
    LeaseReleased,
    MessageRead,
    MessageSent,
    NegotiationTurn,
    NotePosted,
    ProtocolViolation,
    ResolutionApplied,
    ReviewCommentPosted,
    ReviewCommentResolved,
    StandHalted,
    StandRenamed,
    StandResumed,
    StandStarted,
    TaskAssigned,
    TaskPlanned,
    TaskStateChanged,
    WorkstreamLanded,
    WorktreeDelta,
)
from lumberjack.domain.message import Message
from lumberjack.domain.note import Note, ReviewComment
from lumberjack.domain.symbols import RepoMap, SymbolRef
from lumberjack.domain.task import Landed, Pending, Task, TaskSpec
from lumberjack.domain.workstream import StandConfig, Workstream
from lumberjack.ids import (
    AgentId,
    ChannelId,
    CommentId,
    CommitSha,
    ConflictId,
    ContractId,
    LeaseId,
    ProposalId,
    RepoPath,
    Seq,
    StandId,
    TaskId,
    WorkstreamId,
)
from lumberjack.ports.ledger import Ledger

__all__ = ["PeerActivity", "Projections"]


@dataclass(frozen=True, slots=True)
class PeerActivity:
    """What a peer is doing, as far as it overlaps the asking workstream."""

    agent: AgentId
    workstream: WorkstreamId
    task: TaskId
    declared: str
    observed_paths: tuple[RepoPath, ...]
    mode: str
    rationale: str

    def render(self) -> str:
        where = ", ".join(self.observed_paths[:4]) or self.declared
        return f"{self.agent} ({self.mode}) on {where} -- {self.rationale}"


@dataclass(slots=True)
class Projections:
    """Mutable derived state, rebuilt by folding the ledger from ``seq=0``."""

    stand: StandId
    cursor: Seq = field(default=Seq(0))
    goal: str = ""
    name: str = ""
    """What a person calls this run. History is append-only, so renaming adds a label
    rather than rewriting the goal that was originally asked for."""
    pid: int | None = None
    session: int = 1
    """How many supervisor sessions this stand has had. A pause and a continue make two."""
    resumed_from: StandId | None = None
    config: StandConfig | None = None
    base: CommitSha | None = None
    integration_branch: str = ""
    integration_head: CommitSha | None = None
    halted: bool = False

    specs: dict[TaskId, TaskSpec] = field(default_factory=dict)
    tasks: dict[TaskId, Task] = field(default_factory=dict)
    workstreams: dict[WorkstreamId, Workstream] = field(default_factory=dict)

    leases: dict[LeaseId, Lease] = field(default_factory=dict)
    queued: list[Claim] = field(default_factory=list)
    denied: list[Claim] = field(default_factory=list)

    deltas: dict[WorkstreamId, WorktreeDelta] = field(default_factory=dict)
    violations: list[ProtocolViolation] = field(default_factory=list)

    conflicts: dict[ConflictId, ConflictReport] = field(default_factory=dict)
    resolved: dict[ConflictId, ConflictReport] = field(default_factory=dict)
    channels: dict[ChannelId, Channel] = field(default_factory=dict)
    rulings: list[ResolutionApplied] = field(default_factory=list)

    notes: list[Note] = field(default_factory=list)
    comments: dict[CommentId, ReviewComment] = field(default_factory=dict)
    messages: list[tuple[Seq, Message]] = field(default_factory=list)
    read_cursor: dict[AgentId, Seq] = field(default_factory=dict)

    contracts: dict[ContractId, Contract] = field(default_factory=dict)
    proposals: dict[ProposalId, AmendmentProposal] = field(default_factory=dict)

    train: list[WorkstreamId] = field(default_factory=list)
    landed: dict[TaskId, CommitSha] = field(default_factory=dict)
    bounces: dict[WorkstreamId, int] = field(default_factory=lambda: defaultdict(int))
    gate_reports: dict[WorkstreamId, GateRun] = field(default_factory=dict)

    repo_map: RepoMap = field(default_factory=RepoMap)

    degraded: dict[str, ComponentFailed] = field(default_factory=dict)
    """The most recent failure of each harness component, healthy ones absent."""

    # -- folding ---------------------------------------------------------------------

    async def hydrate(self, ledger: Ledger) -> None:
        """Fold every event the ledger holds beyond the cursor.  Idempotent."""
        for envelope in await ledger.read(since=self.cursor):
            self.apply(envelope)

    def apply(self, envelope: Envelope[EventPayload]) -> None:
        self.cursor = max(self.cursor, envelope.seq)
        payload = envelope.payload
        match payload:
            case StandStarted():
                self.goal = payload.goal
                self.pid = payload.pid
                self.resumed_from = payload.resumed_from
                self.config = payload.config
                self.base = payload.base
                self.integration_branch = payload.integration_branch
                self.integration_head = payload.base
            case StandResumed():
                self.halted = False
                self.pid = payload.pid
                self.session = payload.session
            case StandRenamed():
                self.name = payload.name
            case StandHalted():
                self.halted = True
            case ComponentFailed():
                # Last failure wins: what matters downstream is the current state of a
                # component, and the full history is still in the ledger for replay.
                self.degraded[payload.component] = payload
            case TaskPlanned():
                self.specs[payload.spec.task_id] = payload.spec
                self.tasks[payload.spec.task_id] = Pending(spec=payload.spec)
            case TaskAssigned():
                # One lane per task. A second assignment supersedes the first rather
                # than running alongside it -- otherwise a resumed stand shows two of
                # every workstream, and the older one never moves again.
                for existing in self.workstreams.values():
                    if (
                        existing.task == payload.task
                        and existing.workstream_id != payload.workstream.workstream_id
                    ):
                        self.workstreams[existing.workstream_id] = existing.model_copy(
                            update={"active": False}
                        )
                self.workstreams[payload.workstream.workstream_id] = payload.workstream
            case TaskStateChanged():
                self.tasks[payload.task_id] = payload.state
                if isinstance(payload.state, Landed):
                    self.landed[payload.task_id] = payload.state.merge
            case ClaimRequested():
                pass
            case LeaseGrantedEvent():
                self.leases[payload.lease.lease_id] = payload.lease
                self._drop_queued(payload.lease.claim)
            case LeaseQueuedEvent():
                self.queued.append(payload.claim)
            case LeaseDeniedEvent():
                self.denied.append(payload.claim)
            case LeaseReleased() | LeaseExpired():
                self.leases.pop(payload.lease_id, None)
            case WorktreeDelta():
                self.deltas[payload.workstream] = payload
            case ProtocolViolation():
                self.violations.append(payload)
            case ConflictDetected():
                self.conflicts[payload.report.conflict_id] = payload.report
            case ConflictCleared():
                report = self.conflicts.pop(payload.conflict_id, None)
                if report is not None:
                    self.resolved[payload.conflict_id] = report
            case ChannelOpened():
                self.channels[payload.channel_id] = Channel(
                    channel_id=payload.channel_id,
                    conflict_id=payload.conflict_id,
                    participants=payload.participants,
                    opened_at=envelope.at,
                )
            case NegotiationTurn():
                channel = self.channels.get(payload.channel_id)
                if channel is not None:
                    self.channels[payload.channel_id] = channel.with_move(payload.move)
            case ChannelClosed():
                channel = self.channels.get(payload.channel_id)
                if channel is not None:
                    self.channels[payload.channel_id] = channel.closed(ChannelState(payload.state))
            case AccordSigned():
                pass
            case DirectiveIssued():
                pass
            case ResolutionApplied():
                self.rulings.append(payload)
            case NotePosted():
                self.notes.append(payload.note)
            case ReviewCommentPosted():
                self.comments[payload.comment.comment_id] = payload.comment
            case ReviewCommentResolved():
                existing = self.comments.get(payload.comment_id)
                if existing is not None:
                    self.comments[payload.comment_id] = existing.model_copy(
                        update={"resolved": True}
                    )
            case MessageSent():
                self.messages.append((envelope.seq, payload.message))
            case MessageRead():
                previous = self.read_cursor.get(payload.reader, Seq(0))
                self.read_cursor[payload.reader] = max(previous, payload.up_to)
            case ContractFrozen():
                self.contracts[payload.contract.contract_id] = payload.contract
            case AmendmentProposed():
                self.proposals[payload.proposal.proposal_id] = payload.proposal
            case AmendmentAccepted():
                self.contracts[payload.contract.contract_id] = payload.contract
                existing = self.proposals.get(payload.proposal_id)
                if existing is not None:
                    self.proposals[payload.proposal_id] = existing.accepted()
            case LandRequested():
                if payload.workstream not in self.train:
                    self.train.append(payload.workstream)
            case GateRun():
                self.gate_reports[payload.workstream] = payload
            case WorkstreamLanded():
                self.integration_head = payload.integration_head
                self.landed[payload.task] = payload.merge
                if payload.workstream in self.train:
                    self.train.remove(payload.workstream)
            case Bounced():
                self.bounces[payload.workstream] += 1
                if payload.workstream in self.train:
                    self.train.remove(payload.workstream)
            case _ as unreachable:
                assert_never(unreachable)

    def _drop_queued(self, claim: Claim) -> None:
        self.queued = [
            item
            for item in self.queued
            if not (item.claimant == claim.claimant and item.scope == claim.scope)
        ]

    # -- queries ---------------------------------------------------------------------

    def active_leases(self, now: datetime) -> tuple[Lease, ...]:
        return tuple(lease for lease in self.leases.values() if not lease.expired_at(now))

    def leases_of(self, workstream: WorkstreamId, now: datetime) -> tuple[Lease, ...]:
        return tuple(lease for lease in self.active_leases(now) if lease.workstream == workstream)

    def agent_of(self, workstream: WorkstreamId) -> AgentId | None:
        found = self.workstreams.get(workstream)
        return found.agent if found is not None else None

    def workstream_of(self, agent: AgentId) -> WorkstreamId | None:
        for workstream in self.workstreams.values():
            if workstream.agent == agent:
                return workstream.workstream_id
        return None

    def active_workstreams(self) -> tuple[Workstream, ...]:
        return tuple(item for item in self.workstreams.values() if item.active)

    def observed_paths(self, workstream: WorkstreamId) -> tuple[RepoPath, ...]:
        delta = self.deltas.get(workstream)
        return delta.paths if delta is not None else ()

    def observed_symbols(self, workstream: WorkstreamId) -> tuple[SymbolRef, ...]:
        delta = self.deltas.get(workstream)
        return delta.symbols if delta is not None else ()

    def open_conflicts_for(
        self, workstream: WorkstreamId, *, at_least: Severity = Severity.NOTICE
    ) -> tuple[ConflictReport, ...]:
        return tuple(
            report
            for report in self.conflicts.values()
            if report.involves(workstream) and report.severity >= at_least
        )

    def blocking_conflicts(self, workstream: WorkstreamId) -> tuple[ConflictReport, ...]:
        return self.open_conflicts_for(workstream, at_least=Severity.BLOCK)

    def unread_for(self, agent: AgentId) -> tuple[Message, ...]:
        cursor = self.read_cursor.get(agent, Seq(0))
        return tuple(
            message
            for seq, message in self.messages
            if seq > cursor and message.addressed_to(agent) and message.frm != agent
        )

    def peers_of(self, workstream: WorkstreamId, now: datetime) -> tuple[PeerActivity, ...]:
        """Peers whose declared or observed footprint overlaps this workstream's."""
        mine_scopes = [lease.scope for lease in self.leases_of(workstream, now)]
        mine_paths = set(self.observed_paths(workstream))
        activity: dict[WorkstreamId, PeerActivity] = {}
        for lease in self.active_leases(now):
            other = lease.workstream
            if other == workstream:
                continue
            other_paths = set(self.observed_paths(other))
            overlaps = bool(mine_paths & other_paths) or any(
                scopes_overlap(scope, lease.scope) for scope in mine_scopes
            )
            if not overlaps:
                continue
            activity[other] = PeerActivity(
                agent=lease.holder,
                workstream=other,
                task=lease.claim.task,
                declared=lease.scope.describe(),
                observed_paths=tuple(sorted(other_paths & mine_paths) or sorted(other_paths)[:4]),
                mode=lease.mode.value,
                rationale=lease.claim.rationale,
            )
        return tuple(activity.values())

    def comments_for(
        self, workstream: WorkstreamId, *, unresolved_only: bool = True
    ) -> tuple[ReviewComment, ...]:
        """Human comments addressed to this workstream, or scoped to files it touched."""
        touched = set(self.observed_paths(workstream))
        return tuple(
            comment
            for comment in self.comments.values()
            if (not unresolved_only or not comment.resolved)
            and (comment.workstream == workstream or comment.file in touched)
        )

    def comments_on(self, conflict: ConflictId) -> tuple[ReviewComment, ...]:
        return tuple(
            comment for comment in self.comments.values() if comment.conflict_id == conflict
        )

    def contracts_for(self, task: TaskId) -> tuple[Contract, ...]:
        return tuple(
            contract
            for contract in self.contracts.values()
            if contract.provider == task or task in contract.consumers
        )

    def notes_matching(self, workstream: WorkstreamId, limit: int) -> tuple[Note, ...]:
        """Top notes by scope overlap, most recent first.  Bounded on purpose."""
        mine = [lease.scope for lease in self.leases.values() if lease.workstream == workstream]
        scored: list[tuple[int, Note]] = []
        for index, note in enumerate(self.notes):
            if note.scope is None:
                score = 1
            elif any(scopes_overlap(note.scope, scope) for scope in mine):
                score = 3
            else:
                score = 0
            if score:
                scored.append((score * 1000 + index, note))
        scored.sort(key=lambda item: item[0], reverse=True)
        return tuple(note for _, note in scored[:limit])

    def stopped_components(self) -> tuple[ComponentFailed, ...]:
        """Components that gave up.

        A single failure is noise; a loop that has stopped changes what a reasonable
        agent would do, which is why only these reach the awareness digest.
        """
        return tuple(
            failure
            for _, failure in sorted(self.degraded.items())
            if failure.giving_up
        )

    def finished(self) -> bool:
        """Whether this stand is over: halted, or every task reached a terminal state."""
        if self.halted:
            return True
        if not self.tasks:
            return False
        return all(task.kind in ("landed", "blocked", "abandoned") for task in self.tasks.values())

    @property
    def title(self) -> str:
        return self.name or self.goal or str(self.stand)

    def lifecycle(self, alive: bool | None = None) -> str:
        """``live``, ``stale``, ``halted`` or ``finished``.

        ``alive`` is whether the supervisor process still exists, which a projection
        cannot know on its own. Passing ``None`` keeps the older, more forgiving
        reading; passing ``False`` for a stand with work outstanding gives ``stale``,
        which is the difference between a run that is working and one that died.
        """
        if self.halted:
            return "halted"
        if self.finished():
            return "finished"
        return "live" if alive is not False else "stale"

    def outstanding(self) -> tuple[TaskId, ...]:
        """Tasks that have not landed and were not abandoned -- what a session picks up."""
        return tuple(
            task_id
            for task_id, task in self.tasks.items()
            if task.kind not in ("landed", "abandoned")
        )

    def completed_tasks(self) -> frozenset[TaskId]:
        return frozenset(self.landed)

    def summary(self) -> str:
        return (
            f"stand={self.stand} tasks={len(self.tasks)} "
            f"workstreams={len(self.active_workstreams())} "
            f"leases={len(self.leases)} open_conflicts={len(self.conflicts)} "
            f"landed={len(self.landed)}"
        )
