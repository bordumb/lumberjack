"""The observability boundary.

Three primitives -- a span, a counter, a histogram -- plus one domain-shaped call,
``record_usage``, because tokens are the number this project most needs and the one
least well served by a generic counter.

:class:`NullTelemetry` is the default everywhere.  That is what lets the harness run,
and the test suite stay fast, with no OpenTelemetry installed: the SDK is imported by
:mod:`lumberjack.adapters.otel` and by nothing else.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pydantic_ai.usage import RunUsage

    from lumberjack.ids import WorkstreamId

__all__ = ["AttrValue", "NullTelemetry", "Span", "Telemetry"]

AttrValue = str | int | float | bool
"""What a collector will accept as an attribute value without a serialization policy."""


@runtime_checkable
class Span(Protocol):
    """A unit of work in progress.  Attributes known only at the end are ``set`` late."""

    def set(self, **attributes: AttrValue) -> None: ...

    def record_error(self, error: BaseException) -> None: ...


class Telemetry(Protocol):
    def span(self, name: str, **attributes: AttrValue) -> AbstractContextManager[Span]: ...

    def counter(self, name: str, value: int = 1, **attributes: AttrValue) -> None: ...

    def histogram(self, name: str, value: float, **attributes: AttrValue) -> None: ...

    def record_usage(
        self,
        workstream: WorkstreamId,
        usage: RunUsage,
        *,
        agent: str | None = None,
        model: str | None = None,
    ) -> None:
        """Emit ``lj.tokens``.  Accounting is :class:`~lumberjack.core.usage.UsageLedger`'s
        job; this is only the export of it, and it is a no-op when nothing is listening."""
        ...


@dataclass(frozen=True, slots=True)
class NullSpan:
    """Accepts everything, keeps nothing."""

    def set(self, **attributes: AttrValue) -> None:
        _ = attributes

    def record_error(self, error: BaseException) -> None:
        _ = error


NULL_SPAN = NullSpan()


@dataclass(frozen=True, slots=True)
class NullTelemetry:
    """The default.  Instrumenting a call site therefore costs nothing to run untraced."""

    def span(self, name: str, **attributes: AttrValue) -> AbstractContextManager[Span]:
        _ = (name, attributes)
        return nullcontext(NULL_SPAN)

    def counter(self, name: str, value: int = 1, **attributes: AttrValue) -> None:
        _ = (name, value, attributes)

    def histogram(self, name: str, value: float, **attributes: AttrValue) -> None:
        _ = (name, value, attributes)

    def record_usage(
        self,
        workstream: WorkstreamId,
        usage: RunUsage,
        *,
        agent: str | None = None,
        model: str | None = None,
    ) -> None:
        _ = (workstream, usage, agent, model)
