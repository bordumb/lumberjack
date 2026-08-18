"""Glob matching and conservative pattern-vs-pattern overlap.

Two questions are asked of globs in this system:

``matches(pattern, path)``
    Exact, decidable, used whenever real paths are known.

``may_overlap(a, b)``
    Undecidable in general, so this is a *conservative approximation*: it returns
    ``False`` only when the patterns provably cannot share a path, and ``True``
    whenever it cannot prove disjointness.  Over-reporting overlap costs a
    notification; under-reporting costs a lost conflict, so the bias is deliberate.
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from functools import lru_cache

__all__ = ["has_magic", "matches", "may_overlap"]

_MAGIC = frozenset("*?[]{}!")


def has_magic(segment: str) -> bool:
    return any(char in _MAGIC for char in segment)


@lru_cache(maxsize=4096)
def _split(pattern: str) -> tuple[str, ...]:
    return tuple(part for part in pattern.split("/") if part)


def matches(pattern: str, path: str) -> bool:
    """Whether ``path`` is matched by ``pattern``.  ``**`` spans any number of segments."""
    return _match(_split(pattern), _split(path))


def _match(pat: tuple[str, ...], parts: tuple[str, ...]) -> bool:
    if not pat:
        return not parts
    head, rest = pat[0], pat[1:]
    if head == "**":
        if not rest:
            return True
        return any(_match(rest, parts[index:]) for index in range(len(parts) + 1))
    if not parts:
        return False
    if not fnmatchcase(parts[0], head):
        return False
    return _match(rest, parts[1:])


def may_overlap(a: str, b: str) -> bool:
    """Conservative: ``False`` only when the two patterns provably share no path."""
    return _overlap(_split(a), _split(b))


def _overlap(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    if not left and not right:
        return True
    if not left:
        return _only_recursive(right)
    if not right:
        return _only_recursive(left)

    lhead, rhead = left[0], right[0]
    if lhead == "**" or rhead == "**":
        wild, other = (left, right) if lhead == "**" else (right, left)
        tail = wild[1:]
        if not tail:
            return True
        return any(_overlap(tail, other[index:]) for index in range(len(other) + 1))

    if not _segments_may_overlap(lhead, rhead):
        return False
    return _overlap(left[1:], right[1:])


def _only_recursive(pattern: tuple[str, ...]) -> bool:
    return all(segment == "**" for segment in pattern)


def _segments_may_overlap(left: str, right: str) -> bool:
    lmagic, rmagic = has_magic(left), has_magic(right)
    if not lmagic and not rmagic:
        return left == right
    if lmagic and not rmagic:
        return fnmatchcase(right, left)
    if rmagic and not lmagic:
        return fnmatchcase(left, right)
    # Both contain wildcards: intersection is not decided cheaply, so assume overlap.
    return True
