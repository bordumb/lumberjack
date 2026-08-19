# 0002 — Telemetry

> Builds on [0001_SPEC.md](0001_SPEC.md) §13 (Observability) and §14 (Safety).
> Sibling specs: [0003_ux.md](0003_ux.md), [0004_errors.md](0004_errors.md).

## 0. House rules

Binding, and shared with every sibling spec. [0000_house_rules.md](0000_house_rules.md)
gives the reasoning.

- **Logging** is the standard library: `log = logging.getLogger(__name__)`, configured
  once in `cli/` and nowhere else. The ledger records what happened to the *work*; the
  log records what happened to the *process*. `print()` belongs to `cli/` alone.
- **Errors** never disappear. No bare `except Exception: pass` — catch the narrowest
  type, log it, and say why continuing is correct. The taxonomy lives in
  `domain/errors.py`; boundary-specific errors stay with their boundary.
- **Dependencies** are a last resort. If you add one, run `uv lock` and commit the
  lockfile in the same change, and say what you added in your handoff.
- **Layering** from [0001_SPEC.md](0001_SPEC.md) §5 is not advisory. Every new module is
  in `domain/`, `ports/`, `core/`, `adapters/`, `agents/`, `cli/`, `tui/` or `server/`.
  Nothing at package root. Test doubles go in `tests/fakes.py`.
- **Wrap, do not sprinkle.** Behaviour that applies to a whole boundary wraps the port,
  following `adapters/projecting.py`.
- **Verification**: run `uv run ruff check . && uv run ty check && uv run pytest`. If you
  *cannot*, say so immediately via `post_note` and `message` — not in your final summary.
  Reading the code is not verification; say which you did.

## 1. Goal

Make a running stand legible while it runs, and make its cost countable.

The ledger already answers "what happened, and why" after the fact. It cannot answer
"what is happening right now, how long is it taking, and what has it cost me". That is
this spec.

Two deliverables, in priority order:

1. **Usage accounting.** `Budget` in `src/lumberjack/domain/workstream.py` declares
   `max_steps_per_task`, `max_wall_clock` and `max_total_tokens`. Nothing reads any of
   them — `grep -rn Budget src/` returns only export lines. A swarm that silently spends
   is the most expensive defect this project can ship.
2. **Tracing and metrics** over the agent runs, the oracle, the gate and the merge train.

## 2. Why now

- `Budget` is dead code.
- `lj run` prints nothing at all until the stand finishes.
- The oracle runs an O(n²) merge sweep on a debounce and nobody knows its unit cost.
  0001_SPEC.md §18.2 lists "optimal N" as an open question that only measurement can settle.
- A bounced task re-runs a whole worker. Nothing counts how often, so nobody knows
  whether the gate is a safety net or a money pit.

## 3. Design

### 3.1 A port, with a no-op default

```python
# src/lumberjack/ports/telemetry.py
AttrValue = str | int | float | bool


class Telemetry(Protocol):
    def span(self, name: str, **attributes: AttrValue) -> AbstractContextManager[Span]: ...
    def counter(self, name: str, value: int = 1, **attributes: AttrValue) -> None: ...
    def histogram(self, name: str, value: float, **attributes: AttrValue) -> None: ...
    def record_usage(self, workstream: WorkstreamId, usage: RunUsage) -> None: ...
```

`NullTelemetry` is the default, so the harness never requires an OpenTelemetry install
and the test suite stays fast. Add it to `Services` and wire it in `Services.wire`.

### 3.2 Prefer wrapping to sprinkling

Where a whole port can be instrumented at its boundary, wrap it rather than editing
call sites — follow the pattern in `src/lumberjack/adapters/projecting.py`, a dataclass
holding `inner` that delegates every method:

```python
git = TracedGit(inner=git, telemetry=telemetry)
```

`TracedGit` is where the interesting numbers are: `merge_tree` latency is the oracle's
unit cost, and `snapshot` latency is what determines whether the sensor debounce is set
sensibly. Instrument the supervisor's loops directly where wrapping cannot reach.

### 3.3 Agent usage

Pass `instrument=True` to the four agent builders in `src/lumberjack/agents/` so
PydanticAI emits its own spans for model requests and tool calls.

`AgentRunResult.usage()` returns a `RunUsage`. The supervisor already holds every result
(`Supervisor._work`, `_run_negotiation`, `_ask_foreman`, `plan`); record usage there,
keyed by workstream.

### 3.4 What to measure

| Instrument | Kind | Attributes | Why |
|---|---|---|---|
| `lj.agent.run` | span | stand, workstream, agent, task, outcome | wall-clock per worker |
| `lj.oracle.probe_pair` | histogram (ms) | clean | settles "optimal N" |
| `lj.oracle.conflict` | counter | source, severity | is the oracle earning its keep |
| `lj.lease.decision` | counter | outcome, mode | how often intent actually collides |
| `lj.gate.run` | histogram (ms) | passed, first_failure | is the gate the bottleneck |
| `lj.train.integration` | counter | status | bounce rate |
| `lj.negotiation.turns` | histogram | settled, escalated | do peers actually settle |
| `lj.tokens` | counter | workstream, agent, model, kind | the money |

`lj.negotiation.turns` is the one that tests the central bet of this project — that
peers holding local context resolve conflicts better than a manager. Record both the
turn count and the terminal state.

### 3.5 Usage ledger

```python
class UsageTotals(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0
    steps: int = 0
    wall_clock: timedelta = timedelta(0)

    @property
    def total_tokens(self) -> int: ...
    def __add__(self, other: UsageTotals) -> UsageTotals: ...
```

Frozen, additive, cheap to query, keyed by workstream and in aggregate. Keep it in
memory — do not turn every model call into a database write.

**0004 will poll this to enforce `Budget`, and 0003 will render it.** Both need
`totals()` and `for_workstream()`. Freeze those two signatures early, post them to the
blackboard, and use `propose_amendment` if you have to change them.

### 3.6 What to build on

OpenTelemetry, behind an optional extra:

```toml
[project.optional-dependencies]
telemetry = [
  "opentelemetry-sdk>=1.30",
  "opentelemetry-exporter-otlp-proto-http>=1.30",
]
```

`NullTelemetry` is what runs when the extra is absent, so the import of the SDK happens
inside the adapter that needs it and nowhere else. `0001_SPEC.md` §13 mentions Logfire:
it is OTel-compatible and needs no separate integration, so do not add it.

Bridge the standard library logger rather than replacing it. Every module already logs
through `logging.getLogger(__name__)`, and OTel's logging instrumentation picks that up
without a second logging vocabulary in the codebase.

### 3.7 Configuration

Add a `telemetry: TelemetryConfig` field to `StandConfig`. `TelemetryConfig` covers:
enabled (default `False`), exporter (`none | otlp | console`), endpoint, service name,
and whether to capture prompt and response content — default `False`, because
repository content is sensitive and 0001_SPEC.md §14 requires that secrets never leave the
machine.

## 4. Acceptance criteria

- [ ] `NullTelemetry` is the default; `uv run pytest` passes with no OTel installed.
- [ ] `uv sync --extra telemetry` installs the OTel stack; nothing else changes.
- [ ] `uv.lock` is regenerated and committed alongside the `pyproject.toml` change.
- [ ] `UsageLedger.totals()` reports non-zero tokens after a `FunctionModel` run.
- [ ] `TracedGit` records `merge_tree` latency against a real repository.
- [ ] Every instrument in §3.4 is emitted, with a test asserting the attribute names.
- [ ] Prompt and response content is never exported unless explicitly enabled.
- [ ] `uv run ruff check .`, `uv run ty check`, `uv run pytest` all clean.

## 5. Out of scope

- **Enforcing** budgets. You count; 0004 stops the stand. Do not add halting logic.
- Rendering. 0003 owns the CLI and the dashboard; give it numbers, not formatting.
- Cost in currency: model pricing is not the harness's business.

## 6. Tests

`tests/unit/test_telemetry.py` — `NullTelemetry` is inert; `UsageTotals` adds correctly;
a recording fake captures spans, counters and attribute names.

`tests/integration/test_telemetry_wiring.py` — with a recording `Telemetry` in
`Services.wire`, a real oracle probe over the `repo` fixture emits
`lj.oracle.probe_pair`, and a `FunctionModel` worker run yields non-zero usage.

The `services` fixture in `tests/conftest.py` gives you a wired stand over a real git
repository. If you need to change that fixture, other agents depend on it — say so on
the blackboard before you do.
