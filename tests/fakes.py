"""Reusable test doubles.

Wrappers over the ports rather than one-off mocks, so a test can assert on what the
harness *emitted* rather than on how it got there.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from pydantic_ai.usage import RunUsage

from lumberjack.ids import WorkstreamId
from lumberjack.ports.telemetry import AttrValue, Span


@dataclass(slots=True)
class RecordedSpan:
    name: str
    attributes: dict[str, AttrValue] = field(default_factory=dict)
    errors: list[BaseException] = field(default_factory=list)
    ended: bool = False

    def set(self, **attributes: AttrValue) -> None:
        self.attributes.update(attributes)

    def record_error(self, error: BaseException) -> None:
        self.errors.append(error)


@dataclass(slots=True)
class RecordedMetric:
    name: str
    value: float
    attributes: dict[str, AttrValue] = field(default_factory=dict)


@dataclass(slots=True)
class RecordedUsage:
    workstream: WorkstreamId
    usage: RunUsage
    agent: str | None = None
    model: str | None = None


@dataclass(slots=True)
class RecordingTelemetry:
    """A ``Telemetry`` that keeps everything, so tests can assert on attribute names.

    Attribute names are the part of an instrument that a rename breaks silently: the
    dashboard queries them by string and nothing type-checks that.
    """

    spans: list[RecordedSpan] = field(default_factory=list)
    counters: list[RecordedMetric] = field(default_factory=list)
    histograms: list[RecordedMetric] = field(default_factory=list)
    usages: list[RecordedUsage] = field(default_factory=list)

    @contextmanager
    def span(self, name: str, **attributes: AttrValue) -> Iterator[Span]:
        recorded = RecordedSpan(name=name, attributes=dict(attributes))
        self.spans.append(recorded)
        try:
            yield recorded
        finally:
            recorded.ended = True

    def counter(self, name: str, value: int = 1, **attributes: AttrValue) -> None:
        self.counters.append(RecordedMetric(name=name, value=value, attributes=dict(attributes)))

    def histogram(self, name: str, value: float, **attributes: AttrValue) -> None:
        self.histograms.append(RecordedMetric(name=name, value=value, attributes=dict(attributes)))

    def record_usage(
        self,
        workstream: WorkstreamId,
        usage: RunUsage,
        *,
        agent: str | None = None,
        model: str | None = None,
    ) -> None:
        self.usages.append(
            RecordedUsage(workstream=workstream, usage=usage, agent=agent, model=model)
        )

    # -- queries -----------------------------------------------------------------------

    def span_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.spans)

    def spans_named(self, name: str) -> tuple[RecordedSpan, ...]:
        return tuple(item for item in self.spans if item.name == name)

    def counters_named(self, name: str) -> tuple[RecordedMetric, ...]:
        return tuple(item for item in self.counters if item.name == name)

    def histograms_named(self, name: str) -> tuple[RecordedMetric, ...]:
        return tuple(item for item in self.histograms if item.name == name)
