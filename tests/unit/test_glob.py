"""Glob matching, and the deliberate asymmetry of ``may_overlap``."""

from __future__ import annotations

import pytest

from lumberjack.domain.glob import matches, may_overlap


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("src/**/*.py", "src/a/b/c.py", True),
        ("src/**/*.py", "src/a.py", True),
        ("src/*.py", "src/a/b.py", False),
        ("src/*.py", "src/a.py", True),
        ("**", "anything/at/all.py", True),
        ("tests/**", "src/a.py", False),
        ("pkg/core.py", "pkg/core.py", True),
        ("pkg/core.py", "pkg/other.py", False),
    ],
)
def test_matches(pattern: str, path: str, expected: bool) -> None:
    assert matches(pattern, path) is expected


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("src/**/*.py", "src/core/broker.py", True),
        ("src/core/*.py", "tests/**", False),
        ("src/a.py", "src/b.py", False),
        ("src/a.py", "src/a.py", True),
        ("**", "anything.py", True),
        ("src/*/models.py", "src/*/views.py", False),
    ],
)
def test_may_overlap(left: str, right: str, expected: bool) -> None:
    assert may_overlap(left, right) is expected


def test_may_overlap_is_symmetric() -> None:
    pairs = [("src/**", "src/a/b.py"), ("a/*.py", "b/*.py"), ("**/*.py", "pkg/x.py")]
    for left, right in pairs:
        assert may_overlap(left, right) == may_overlap(right, left)


def test_may_overlap_errs_towards_reporting() -> None:
    """Two wildcard patterns are not decided cheaply, so we assume they might meet.

    Over-reporting costs a notification; under-reporting costs a missed conflict.
    """
    assert may_overlap("src/*a*.py", "src/*b*.py") is True
