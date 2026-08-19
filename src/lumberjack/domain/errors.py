"""The error taxonomy.

An error is vocabulary before it is control flow, so the hierarchy lives in ``domain/``
where ``core/`` and ``adapters/`` can both raise from it.  Errors that belong to one
boundary stay in that boundary's module and subclass :class:`LumberjackError` there --
``GitError`` in ``ports/git.py`` is the example.

The only distinction the code acts on is **transient vs terminal**:

transient
    the same call, tried again in a moment, could plausibly succeed -- a git index lock
    held by a sibling worktree, a model provider that is overloaded, a gate that timed
    out once.
terminal
    trying again changes nothing -- a protocol violation, an invalid ruling, a corrupt
    ledger row, a worktree that has vanished from disk.

A merge conflict is neither: it is an *answer*.  Nothing in here should ever be raised
for one, and :data:`GIT_TRANSIENT_MARKERS` deliberately contains no conflict wording, so
a retry wrapper built on it cannot re-run a ``merge-tree`` that told the truth.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import timedelta

__all__ = [
    "GIT_TRANSIENT_MARKERS",
    "Backoff",
    "BudgetExhaustedError",
    "CoordinationUnavailableError",
    "GateHungError",
    "GateTimeoutError",
    "IndexLockedError",
    "InvalidRulingError",
    "LedgerCorruptError",
    "LumberjackError",
    "ProtocolBreachError",
    "ProviderOverloadedError",
    "TerminalError",
    "TransientError",
    "WorktreeVanishedError",
    "git_stderr_is_transient",
    "is_transient",
]


class LumberjackError(Exception):
    """The base of every error this project raises deliberately."""

    transient: bool = False


class TransientError(LumberjackError):
    """Retrying could plausibly succeed."""

    transient = True


class TerminalError(LumberjackError):
    """Retrying changes nothing; the caller has to do something else."""

    transient = False


# -- transient -----------------------------------------------------------------------


class IndexLockedError(TransientError):
    """A sibling worktree holds the git index or a ref lock.

    Real, and common: every worktree in a stand shares one object store.
    """


class ProviderOverloadedError(TransientError):
    """A model provider rate-limited us or reported itself overloaded."""


class GateTimeoutError(TransientError):
    """The gate did not finish in time.

    Once is a slow machine.  Twice in a row is a hung test suite, which is
    :class:`GateHungError` and terminal.
    """


# -- terminal ------------------------------------------------------------------------


class ProtocolBreachError(TerminalError):
    """An agent did something the coordination protocol forbids."""


class InvalidRulingError(TerminalError):
    """An arbitration produced a resolution that cannot be applied."""


class LedgerCorruptError(TerminalError):
    """A ledger row could not be decoded into an event."""


class WorktreeVanishedError(TerminalError):
    """A workstream's worktree is no longer on disk."""


class GateHungError(TerminalError):
    """The gate timed out twice running.  That is a hung suite, not a slow one."""


class BudgetExhaustedError(TerminalError):
    """A :class:`~lumberjack.domain.workstream.Budget` limit was reached."""


class CoordinationUnavailableError(TerminalError):
    """The infrastructure a swarm depends on is missing or refusing.

    Raised loudly and early.  A stand that runs without coordination still burns
    tokens and still writes code -- it just writes it blind, while the harness reports
    healthy progress the whole time.
    """


# -- classification ------------------------------------------------------------------


def is_transient(error: BaseException) -> bool:
    """Whether retrying ``error`` could plausibly succeed.

    Anything the project raises answers for itself.  Beyond that only the three stdlib
    failures that genuinely describe contention are treated as retryable; guessing more
    widely is how a retry loop ends up hammering a deterministic bug.
    """
    if isinstance(error, LumberjackError):
        return error.transient
    return isinstance(error, TimeoutError | ConnectionError | BlockingIOError)


GIT_TRANSIENT_MARKERS: tuple[str, ...] = (
    "index.lock",
    ".lock': file exists",
    "unable to lock",
    "cannot lock ref",
    "could not lock config file",
    "another git process seems to be running",
    "resource temporarily unavailable",
)
"""The allowlist of git failures worth retrying: lock contention, and nothing else.

Every entry names a *lock*.  Conflicts, missing refs, dirty worktrees and bad arguments
are all answers to the question that was asked, and retrying them only wastes time.
"""


def git_stderr_is_transient(stderr: str) -> bool:
    """Whether git's stderr matches the documented retry allowlist."""
    lowered = stderr.lower()
    return any(marker in lowered for marker in GIT_TRANSIENT_MARKERS)


# -- backoff -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Backoff:
    """Bounded exponential backoff with jitter.

    Bounded because an unbounded retry is a hang with extra steps, and jittered because
    N worktrees contending for one index lock will otherwise wake up together and
    contend again.
    """

    attempts: int = 4
    base: timedelta = timedelta(milliseconds=50)
    cap: timedelta = timedelta(seconds=2)
    jitter: float = 0.5
    """Fraction of each delay that is randomised, in ``[0, 1]``."""

    def __post_init__(self) -> None:
        if self.attempts < 1:
            msg = "attempts must be at least 1"
            raise ValueError(msg)
        if not 0.0 <= self.jitter <= 1.0:
            msg = "jitter must be between 0 and 1"
            raise ValueError(msg)
        if self.cap < self.base:
            msg = "cap must be at least base"
            raise ValueError(msg)

    def delay(self, attempt: int, *, entropy: float | None = None) -> timedelta:
        """The wait before attempt ``attempt`` (1-based, so ``delay(1)`` is the first).

        ``entropy`` is the randomness, exposed so a test can pin it.  The result is
        always in ``[full * (1 - jitter), full]`` and never above :attr:`cap`.
        """
        step = max(1, attempt)
        full = min(self.cap.total_seconds(), self.base.total_seconds() * 2 ** (step - 1))
        roll = random.random() if entropy is None else min(max(entropy, 0.0), 1.0)
        return timedelta(seconds=full * (1.0 - self.jitter + self.jitter * roll))

    def total_at_most(self) -> timedelta:
        """The longest this policy can ever sleep in one retry sequence."""
        return sum(
            (self.delay(attempt, entropy=1.0) for attempt in range(1, self.attempts)),
            timedelta(0),
        )
