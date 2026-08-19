"""The OpenTelemetry adapter.

The SDK is imported *here* and nowhere else, inside the functions that need it, so a
checkout without the ``telemetry`` extra runs on :class:`NullTelemetry` and never sees
an ImportError.  ``uv sync --extra telemetry`` is the only thing that changes.

Logfire needs no separate integration -- it speaks OTLP -- so pointing ``endpoint`` at
it is the whole story, and there is no second logging vocabulary in the codebase: the
standard library logger every module already uses is bridged, not replaced.
"""

from __future__ import annotations

import logging
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from lumberjack.domain.telemetry import Exporter, TelemetryConfig
from lumberjack.ports.telemetry import AttrValue, NullTelemetry, Span, Telemetry

if TYPE_CHECKING:
    from pydantic_ai.usage import RunUsage

    from lumberjack.ids import WorkstreamId

log = logging.getLogger(__name__)

__all__ = ["OtelTelemetry", "build_telemetry"]


def build_telemetry(config: TelemetryConfig) -> Telemetry:
    """The one place that decides whether anything is exported at all.

    A missing SDK is a configuration mistake, not a reason to abort a stand that is
    otherwise ready to run -- so it is logged loudly and the stand carries on counting
    in memory, which is what :class:`~lumberjack.core.usage.UsageLedger` does anyway.
    """
    if not config.enabled or config.exporter is Exporter.NONE:
        return NullTelemetry()
    try:
        return OtelTelemetry.build(config)
    except ImportError as error:
        log.error(
            "telemetry is enabled but the OpenTelemetry SDK is not installed (%s); "
            "run `uv sync --extra telemetry`. Continuing untraced.",
            error,
        )
        return NullTelemetry()


@dataclass(slots=True)
class OtelSpan:
    """Adapts an OTel span to the port's two-method surface."""

    inner: Any

    def set(self, **attributes: AttrValue) -> None:
        for key, value in attributes.items():
            self.inner.set_attribute(key, value)

    def record_error(self, error: BaseException) -> None:
        self.inner.record_exception(error)


@dataclass(slots=True)
class OtelTelemetry:
    """Spans and metrics over the OTel SDK.

    ``capture_content`` is off by default and is never consulted here for a reason:
    this class only ever emits counts, names and durations.  Prompt and response text
    reaches a collector solely through PydanticAI's own instrumentation, which
    :meth:`build` configures explicitly.
    """

    tracer: Any
    meter: Any
    capture_content: bool = False
    _counters: dict[str, Any] = field(default_factory=dict)
    _histograms: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def build(cls, config: TelemetryConfig) -> OtelTelemetry:
        """Raises :class:`ImportError` when the ``telemetry`` extra is not installed."""
        from opentelemetry import metrics, trace
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create({"service.name": config.service_name})
        span_exporter, metric_exporter = _exporters(config)

        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
        trace.set_tracer_provider(tracer_provider)

        meter_provider = MeterProvider(
            resource=resource,
            metric_readers=[PeriodicExportingMetricReader(metric_exporter)],
        )
        metrics.set_meter_provider(meter_provider)

        _bridge_logging(resource, config)
        _instrument_pydantic_ai(capture_content=config.capture_content)

        return cls(
            tracer=trace.get_tracer("lumberjack"),
            meter=metrics.get_meter("lumberjack"),
            capture_content=config.capture_content,
        )

    def span(self, name: str, **attributes: AttrValue) -> AbstractContextManager[Span]:
        started = self.tracer.start_as_current_span(name, attributes=dict(attributes))
        return _SpanScope(started)

    def counter(self, name: str, value: int = 1, **attributes: AttrValue) -> None:
        instrument = self._counters.get(name)
        if instrument is None:
            instrument = self.meter.create_counter(name)
            self._counters[name] = instrument
        instrument.add(value, dict(attributes))

    def histogram(self, name: str, value: float, **attributes: AttrValue) -> None:
        instrument = self._histograms.get(name)
        if instrument is None:
            instrument = self.meter.create_histogram(name)
            self._histograms[name] = instrument
        instrument.record(value, dict(attributes))

    def record_usage(
        self,
        workstream: WorkstreamId,
        usage: RunUsage,
        *,
        agent: str | None = None,
        model: str | None = None,
    ) -> None:
        common: dict[str, AttrValue] = {
            "workstream": str(workstream),
            "agent": agent or "unknown",
            "model": model or "unknown",
        }
        for kind, value in (
            ("input", usage.input_tokens),
            ("output", usage.output_tokens),
            ("cache_read", usage.cache_read_tokens),
            ("cache_write", usage.cache_write_tokens),
        ):
            if value:
                self.counter("lj.tokens", value, kind=kind, **common)


@dataclass(slots=True)
class _SpanScope:
    """Bridges OTel's context manager, which yields its own span type, to :class:`Span`."""

    inner: AbstractContextManager[Any]

    def __enter__(self) -> Span:
        return OtelSpan(inner=self.inner.__enter__())

    def __exit__(self, *exc: Any) -> bool | None:
        return self.inner.__exit__(*exc)


def _exporters(config: TelemetryConfig) -> tuple[Any, Any]:
    if config.exporter is Exporter.CONSOLE:
        from opentelemetry.sdk.metrics.export import ConsoleMetricExporter
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter

        return ConsoleSpanExporter(), ConsoleMetricExporter()

    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    if config.endpoint is None:
        return OTLPSpanExporter(), OTLPMetricExporter()
    base = config.endpoint.rstrip("/")
    return (
        OTLPSpanExporter(endpoint=f"{base}/v1/traces"),
        OTLPMetricExporter(endpoint=f"{base}/v1/metrics"),
    )


def _bridge_logging(resource: Any, config: TelemetryConfig) -> None:
    """Ship the standard library logger, rather than asking modules to log twice."""
    from opentelemetry._logs import set_logger_provider
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

    provider = LoggerProvider(resource=resource)
    if config.exporter is Exporter.CONSOLE:
        from opentelemetry.sdk._logs.export import ConsoleLogExporter

        exporter: Any = ConsoleLogExporter()
    else:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter

        endpoint = config.endpoint
        exporter = (
            OTLPLogExporter()
            if endpoint is None
            else OTLPLogExporter(endpoint=f"{endpoint.rstrip('/')}/v1/logs")
        )
    provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
    set_logger_provider(provider)
    # A handler on the root logger, not a reconfiguration of it: `cli/` still owns
    # levels and formatters, and this only adds a second destination.
    logging.getLogger().addHandler(LoggingHandler(logger_provider=provider))


def _instrument_pydantic_ai(*, capture_content: bool) -> None:
    """Turn on PydanticAI's own spans, with content capture explicitly decided.

    The default here is the safe one, and it is stated rather than inherited: an
    upstream default that flips would otherwise start exporting repository content.
    """
    try:
        from pydantic_ai.agent import Agent
        from pydantic_ai.models.instrumented import InstrumentationSettings
    except ImportError:  # pragma: no cover -- pydantic-ai is a hard dependency
        log.warning("pydantic-ai instrumentation settings unavailable; agent spans are off")
        return
    Agent.instrument_all(InstrumentationSettings(include_content=capture_content))
