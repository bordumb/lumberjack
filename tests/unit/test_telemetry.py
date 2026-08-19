"""The telemetry primitives: inert by default, additive totals, named instruments."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError
from pydantic_ai.usage import RunUsage

from lumberjack.agents.instrumentation import QUIET, instrumentation
from lumberjack.core.usage import FOREMAN_USAGE_KEY, UsageLedger
from lumberjack.domain.telemetry import Exporter, TelemetryConfig
from lumberjack.domain.usage import UsageTotals
from lumberjack.domain.workstream import StandConfig
from lumberjack.ids import WorkstreamId
from lumberjack.ports.telemetry import NullTelemetry
from tests.fakes import RecordingTelemetry

WS = WorkstreamId("ws-one")
OTHER = WorkstreamId("ws-two")


def test_null_telemetry_accepts_every_call_and_keeps_nothing():
    telemetry = NullTelemetry()
    with telemetry.span("lj.agent.run", stand="s", workstream="w") as span:
        span.set(outcome="TaskCompleted")
        span.record_error(RuntimeError("noted and dropped"))
    telemetry.counter("lj.train.integration", status="landed")
    telemetry.histogram("lj.gate.run", 12.5, passed=True)
    telemetry.record_usage(WS, RunUsage(input_tokens=1, output_tokens=2))


def test_a_null_span_is_still_a_span():
    """The no-op path must satisfy the same protocol, or instrumenting is conditional."""
    from lumberjack.ports.telemetry import Span

    with NullTelemetry().span("lj.agent.run") as span:
        assert isinstance(span, Span)


def test_usage_totals_add_field_by_field():
    left = UsageTotals(
        input_tokens=10,
        output_tokens=4,
        cache_read_tokens=2,
        cache_write_tokens=1,
        requests=2,
        steps=2,
        tool_calls=3,
        wall_clock=timedelta(seconds=5),
    )
    right = UsageTotals(input_tokens=5, output_tokens=1, requests=1, steps=1)
    both = left + right

    assert both.input_tokens == 15
    assert both.output_tokens == 5
    assert both.cache_read_tokens == 2
    assert both.requests == 3
    assert both.steps == 3
    assert both.tool_calls == 3
    assert both.wall_clock == timedelta(seconds=5)


def test_total_tokens_counts_cache_traffic_too():
    """Cached tokens are billed.  Leaving them out understates the bill it exists to show."""
    totals = UsageTotals(
        input_tokens=10, output_tokens=4, cache_read_tokens=100, cache_write_tokens=7
    )
    assert totals.total_tokens == 121


def test_usage_totals_are_frozen():
    """Additive, never mutated: two readers of one tally must not race each other.

    Assigned through ``setattr`` because the direct form is a type error, and the
    behaviour under test is the runtime one.
    """
    totals = UsageTotals(input_tokens=1)
    with pytest.raises(ValidationError):
        setattr(totals, "input_tokens", 9)  # noqa: B010


def test_an_unknown_workstream_has_spent_nothing():
    """0004 polls this on a timer; raising on a workstream that has not run yet would
    turn budget enforcement into a source of crashes."""
    ledger = UsageLedger()
    assert ledger.for_workstream(WorkstreamId("never-seen")) == UsageTotals()
    assert ledger.totals() == UsageTotals()


def test_recording_usage_accumulates_per_workstream_and_in_aggregate():
    ledger = UsageLedger()
    ledger.record(WS, RunUsage(input_tokens=100, output_tokens=20, requests=2))
    ledger.record(WS, RunUsage(input_tokens=50, output_tokens=10, requests=1))
    ledger.record(OTHER, RunUsage(input_tokens=7, output_tokens=1, requests=1))

    assert ledger.for_workstream(WS).input_tokens == 150
    assert ledger.for_workstream(WS).total_tokens == 180
    assert ledger.for_workstream(OTHER).total_tokens == 8
    assert ledger.totals().total_tokens == 188
    assert ledger.totals().requests == 4
    assert set(ledger.workstreams()) == {WS, OTHER}


def test_steps_default_to_the_request_count():
    """``Budget.max_steps_per_task`` is compared against this, so the mapping is asserted."""
    ledger = UsageLedger()
    ledger.record(WS, RunUsage(input_tokens=1, output_tokens=1, requests=7))
    assert ledger.for_workstream(WS).steps == 7

    ledger.record(OTHER, RunUsage(input_tokens=1, output_tokens=1, requests=7), steps=1)
    assert ledger.for_workstream(OTHER).steps == 1


def test_recording_usage_exports_lj_tokens_split_by_kind():
    telemetry = RecordingTelemetry()
    ledger = UsageLedger(telemetry=telemetry)
    ledger.record(
        WS,
        RunUsage(input_tokens=100, output_tokens=20, cache_read_tokens=5),
        agent="agent-a",
        model="anthropic:claude-opus-5",
    )

    assert len(telemetry.usages) == 1
    recorded = telemetry.usages[0]
    assert recorded.workstream == WS
    assert recorded.agent == "agent-a"
    assert recorded.model == "anthropic:claude-opus-5"


def test_foreman_usage_has_its_own_key():
    """Planning happens before any workstream exists; charging it to one misattributes it."""
    ledger = UsageLedger()
    ledger.record(FOREMAN_USAGE_KEY, RunUsage(input_tokens=9, output_tokens=1, requests=1))
    assert ledger.for_workstream(FOREMAN_USAGE_KEY).total_tokens == 10
    assert ledger.for_workstream(WS) == UsageTotals()


def test_wall_clock_can_be_folded_in_without_a_model_run():
    ledger = UsageLedger()
    ledger.add(WS, UsageTotals(wall_clock=timedelta(seconds=90)))
    assert ledger.totals().wall_clock == timedelta(seconds=90)


# -- the recording double ------------------------------------------------------------


def test_the_recording_telemetry_captures_spans_counters_and_attribute_names():
    telemetry = RecordingTelemetry()
    with telemetry.span("lj.agent.run", stand="s", workstream="w", agent="a", task="t") as span:
        span.set(outcome="TaskCompleted")
    telemetry.counter("lj.train.integration", status="landed")
    telemetry.histogram("lj.oracle.probe_pair", 3.25, clean=True, prefiltered=False)

    recorded = telemetry.spans_named("lj.agent.run")[0]
    assert set(recorded.attributes) == {"stand", "workstream", "agent", "task", "outcome"}
    assert recorded.ended
    assert telemetry.counters_named("lj.train.integration")[0].attributes == {"status": "landed"}
    probe = telemetry.histograms_named("lj.oracle.probe_pair")[0]
    assert probe.value == 3.25
    assert probe.attributes == {"clean": True, "prefiltered": False}


def test_a_span_records_the_error_and_still_closes():
    telemetry = RecordingTelemetry()
    error = RuntimeError("worker died")
    with telemetry.span("lj.agent.run") as span:
        span.record_error(error)
    recorded = telemetry.spans_named("lj.agent.run")[0]
    assert recorded.errors == [error]
    assert recorded.ended


# -- configuration -------------------------------------------------------------------


def test_telemetry_is_off_and_quiet_by_default():
    config = StandConfig()
    assert config.telemetry.enabled is False
    assert config.telemetry.exporter is Exporter.NONE
    assert config.telemetry.capture_content is False


def test_enabling_telemetry_without_an_exporter_is_rejected():
    """Otherwise the operator turns it on, sees nothing, and blames the harness."""
    with pytest.raises(ValidationError):
        TelemetryConfig(enabled=True, exporter=Exporter.NONE)


def test_a_disabled_config_builds_the_null_telemetry():
    from lumberjack.adapters.otel import build_telemetry

    assert isinstance(build_telemetry(TelemetryConfig()), NullTelemetry)


class _FakeInstrument:
    def __init__(self) -> None:
        self.adds: list[tuple[int, dict[str, object]]] = []
        self.records: list[tuple[float, dict[str, object]]] = []

    def add(self, value, attributes):
        self.adds.append((value, attributes))

    def record(self, value, attributes):
        self.records.append((value, attributes))


class _FakeMeter:
    def __init__(self) -> None:
        self.instruments: dict[str, _FakeInstrument] = {}

    def _named(self, name: str) -> _FakeInstrument:
        return self.instruments.setdefault(name, _FakeInstrument())

    def create_counter(self, name: str) -> _FakeInstrument:
        return self._named(name)

    def create_histogram(self, name: str) -> _FakeInstrument:
        return self._named(name)


def test_lj_tokens_is_emitted_per_kind_with_the_attributes_0002_names():
    """§3.4's last row, and the only one that answers "what did this cost"."""
    from lumberjack.adapters.otel import OtelTelemetry

    meter = _FakeMeter()
    telemetry = OtelTelemetry(tracer=None, meter=meter)
    telemetry.record_usage(
        WS,
        RunUsage(input_tokens=100, output_tokens=20, cache_read_tokens=5, requests=1),
        agent="agent-a",
        model="anthropic:claude-opus-5",
    )

    tokens = meter.instruments["lj.tokens"]
    assert [value for value, _ in tokens.adds] == [100, 20, 5]
    for _, attributes in tokens.adds:
        assert set(attributes) == {"workstream", "agent", "model", "kind"}
        assert attributes["workstream"] == WS
        assert attributes["model"] == "anthropic:claude-opus-5"
    assert [attributes["kind"] for _, attributes in tokens.adds] == [
        "input",
        "output",
        "cache_read",
    ]


def test_an_instrument_is_created_once_and_reused():
    """A fresh counter per call is a memory leak with a metrics endpoint attached."""
    from lumberjack.adapters.otel import OtelTelemetry

    meter = _FakeMeter()
    telemetry = OtelTelemetry(tracer=None, meter=meter)
    telemetry.counter("lj.train.integration", status="landed")
    telemetry.counter("lj.train.integration", status="bounced")
    telemetry.histogram("lj.gate.run", 1.0, passed=True)

    assert len(meter.instruments["lj.train.integration"].adds) == 2
    assert set(meter.instruments) == {"lj.train.integration", "lj.gate.run"}


def test_agent_instrumentation_never_captures_content_unless_asked():
    """0001_SPEC.md §14: a prompt here contains repository source."""
    assert QUIET.include_content is False
    assert QUIET.include_binary_content is False
    assert instrumentation(capture_content=True).include_content is True


def test_the_built_agents_are_instrumented_and_quiet():
    from lumberjack.agents.foreman import build_arbiter, build_planner
    from lumberjack.agents.negotiator import build_negotiator
    from lumberjack.agents.worker import build_worker

    for agent in (build_worker(), build_planner(), build_arbiter(), build_negotiator()):
        settings = agent.instrument
        assert settings is not False and settings is not None
        assert not isinstance(settings, bool)
        assert settings.include_content is False
