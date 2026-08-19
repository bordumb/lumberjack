"""The taxonomy, transient classification, and the bounds on backoff."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from lumberjack.domain.errors import (
    GIT_TRANSIENT_MARKERS,
    Backoff,
    BudgetExhaustedError,
    CoordinationUnavailableError,
    GateHungError,
    GateTimeoutError,
    IndexLockedError,
    LumberjackError,
    ProviderOverloadedError,
    TerminalError,
    TransientError,
    git_stderr_is_transient,
    is_transient,
)
from lumberjack.ports.git import GitError


def test_every_error_is_one_of_transient_or_terminal() -> None:
    for error in (IndexLockedError, ProviderOverloadedError, GateTimeoutError):
        assert issubclass(error, TransientError)
        assert error().transient is True
    for error in (GateHungError, BudgetExhaustedError, CoordinationUnavailableError):
        assert issubclass(error, TerminalError)
        assert error().transient is False


def test_a_gate_that_times_out_twice_is_a_different_error_from_one_that_times_out_once() -> None:
    """Once is a slow machine; twice running is a hung suite, and not worth retrying."""
    assert is_transient(GateTimeoutError("first")) is True
    assert is_transient(GateHungError("second")) is False


def test_unknown_exceptions_are_terminal_unless_they_describe_contention() -> None:
    assert is_transient(ValueError("nope")) is False
    assert is_transient(RuntimeError("nope")) is False
    assert is_transient(TimeoutError()) is True
    assert is_transient(ConnectionError()) is True


def test_git_error_is_part_of_the_taxonomy_and_classifies_itself() -> None:
    """`GitError` keeps its name and its home, and answers the only question asked of it."""
    locked = GitError(("snapshot",), 128, "fatal: Unable to create '.git/index.lock': File exists.")
    missing = GitError(("rev-parse",), 128, "fatal: not a git repository")

    assert isinstance(locked, LumberjackError)
    assert locked.transient is True
    assert missing.transient is False


def test_a_merge_conflict_is_never_classified_as_retryable() -> None:
    """A conflict is an answer.  Retrying one asks git the same question again."""
    conflicts = (
        "CONFLICT (content): Merge conflict in pkg/core.py",
        "Auto-merging pkg/core.py\nCONFLICT (add/add): Merge conflict in README.md",
        "error: could not apply 4b825dc... work; conflicts in pkg/core.py",
    )
    for stderr in conflicts:
        assert git_stderr_is_transient(stderr) is False
        assert GitError(("merge-tree",), 1, stderr).transient is False


def test_the_git_allowlist_is_only_contention() -> None:
    """Every entry describes a lock somebody else holds.  Nothing describes an answer."""
    for marker in GIT_TRANSIENT_MARKERS:
        assert not any(word in marker for word in ("conflict", "merge", "rebase", "revision"))
    assert git_stderr_is_transient("fatal: Unable to create index.lock: File exists") is True
    assert git_stderr_is_transient("Another git process seems to be running") is True
    assert git_stderr_is_transient("fatal: bad revision 'nope'") is False


def test_backoff_delays_grow_but_stay_under_the_cap() -> None:
    backoff = Backoff(attempts=6, base=timedelta(milliseconds=10), cap=timedelta(milliseconds=80))
    full = [backoff.delay(attempt, entropy=1.0) for attempt in range(1, 7)]

    assert full == sorted(full), "delays must not shrink as attempts go up"
    assert full[0] == timedelta(milliseconds=10)
    assert max(full) <= backoff.cap
    assert full[-1] == backoff.cap, "the cap is reached, not exceeded"


def test_backoff_jitter_stays_inside_its_own_bounds() -> None:
    """Jitter exists so N worktrees stop retrying in lockstep, not to unbound the wait."""
    backoff = Backoff(attempts=4, base=timedelta(milliseconds=100), cap=timedelta(seconds=1))
    for attempt in range(1, 5):
        ceiling = backoff.delay(attempt, entropy=1.0)
        floor = backoff.delay(attempt, entropy=0.0)
        assert floor == ceiling / 2, "jitter=0.5 halves the delay at worst"
        for _ in range(20):
            sampled = backoff.delay(attempt)
            assert floor <= sampled <= ceiling


def test_backoff_total_is_bounded() -> None:
    """An unbounded retry is a hang with extra steps."""
    backoff = Backoff(attempts=5, base=timedelta(milliseconds=50), cap=timedelta(seconds=1))
    assert backoff.total_at_most() <= timedelta(seconds=4)


def test_backoff_rejects_incoherent_policies() -> None:
    with pytest.raises(ValueError, match="attempts"):
        Backoff(attempts=0)
    with pytest.raises(ValueError, match="jitter"):
        Backoff(jitter=1.5)
    with pytest.raises(ValueError, match="cap"):
        Backoff(base=timedelta(seconds=10), cap=timedelta(seconds=1))


def test_no_exception_is_swallowed_without_a_reason() -> None:
    """The shape of every invisible failure this project has had.

    A bare ``except Exception`` or ``suppress(Exception)`` is allowed only where a
    comment says why continuing is correct -- and asserting on the shape is the only way
    that stays true once the next person is in a hurry.
    """
    root = Path(__file__).resolve().parents[2] / "src" / "lumberjack"
    offenders: list[str] = []
    for module in sorted(root.rglob("*.py")):
        lines = module.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith(("#", '"', "'")) or "``" in line:
                continue  # prose about the rule, not the rule being broken
            if "except Exception" not in stripped and "suppress(Exception" not in stripped:
                continue
            window = lines[max(0, number - 5) : number + 6]
            if not any(item.strip().startswith("#") for item in window):
                offenders.append(f"{module.name}:{number + 1}: {stripped}")
    assert not offenders, "swallowed without a reason:\n" + "\n".join(offenders)
