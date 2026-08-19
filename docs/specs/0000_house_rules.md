# 0000 — House rules

Standards every spec in this directory inherits. They exist because the first run of
0002, 0003 and 0004 showed that a spec which says *what* to build but not *how to build
it here* gets three different answers to the same question.

Each spec restates the binding parts in its own §0. This document is the reasoning.

---

## 1. Logging

Use the standard library.

```python
import logging

log = logging.getLogger(__name__)
```

No dependency, one logger per module, and OpenTelemetry can bridge it when 0002 lands.
Library code never configures logging: `cli/` calls `logging.basicConfig` once, and
nothing else touches handlers, levels or formatters.

**The rule that matters is what goes where:**

| | |
|---|---|
| **The ledger** | what happened to the *work* — claims, conflicts, landings, rulings |
| **The log** | what happened to the *process* — a subprocess that died, a retry, a config that could not be read |

A failure that only reaches the ledger is invisible to anyone reading a terminal. A
failure that only reaches the log never appears in the digest, so no agent learns about
it. Serious failures go to both, and the event is the one that carries the detail.

`print()` is for `cli/` only, where the output *is* the product. Anywhere else it is a
log line that lost its level and its context.

## 2. Errors

The taxonomy lives in `src/lumberjack/domain/errors.py`, because an error is domain
vocabulary before it is a control-flow mechanism. Errors specific to one boundary stay
in that boundary's module and subclass the base — `GitError` remains in `ports/git.py`.

Never swallow an exception silently. If a failure is genuinely acceptable, catch the
narrowest type that can occur, log it, and say in a comment why continuing is correct.
`except Exception: pass` is the shape of every invisible failure this project has had.

## 3. Dependencies

Prefer the standard library. This project reads git by shelling out and stores state in
SQLite rather than taking a driver, and that has cost nothing.

When a dependency is genuinely warranted:

1. add it to `pyproject.toml` under the group it belongs to;
2. run `uv lock` and **commit the lockfile in the same change** — a `pyproject.toml`
   that has moved ahead of `uv.lock` re-resolves on the next `uv run`, for everyone;
3. say in your handoff note what you added and why.

Optional features go behind an extra, and the code must work with the extra absent.

## 4. Where new modules go

`0001_SPEC.md` §5 declares the layering, and it is not advisory:

```
domain/    pure types; no I/O, no imports beyond pydantic and the stdlib
ports/     Protocols for every I/O boundary
core/      logic over ports
adapters/  concrete implementations
agents/    PydanticAI agents and toolsets
cli/ tui/ server/   operator and machine surfaces
```

Every new module belongs to exactly one of these. Nothing goes at package root. If a
module seems not to fit, it is usually two modules.

Test doubles live in `tests/fakes.py`, not in `src/` — they are not part of the product.

## 5. Wrapping beats sprinkling

Where behaviour applies to a whole boundary, wrap the port rather than editing every
call site. `adapters/projecting.py` is the pattern: a dataclass holding `inner` that
delegates every method. Tracing, retrying and fault injection are all this shape, and it
keeps the change reviewable and the core untouched.

## 6. Verifying your work

Run the checks:

```bash
uv run ruff check . && uv run ty check && uv run pytest
```

**If you cannot run them, say so immediately** — `post_note` on the `gotchas` topic and
`message` the agents whose work touches yours. Do not save it for your final summary. An
unverified branch that everyone believes is verified is worse than a red one, and the
merge train will run the gate on integration either way.

Reading the code is not verification. Say which you did.

## 7. Tests

Unit tests in `tests/unit/`, integration in `tests/integration/`. Name the behaviour,
not the function: `test_a_denied_coordination_call_blocks_the_task`, not `test_run`.

A test that would pass against the bug it is meant to catch is not a test. Where a
failure has a shape — a swallowed exception, a silent default — assert on the shape.
