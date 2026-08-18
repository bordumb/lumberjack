# 0004 — Errors, resilience and limits

> Builds on [0001_SPEC.md](0001_SPEC.md) §14 (Safety), §19.3 (concurrency bugs found while building).
> Sibling specs: [0002_telemetry.md](0002_telemetry.md), [0003_ux.md](0003_ux.md).

## 1. Goal

Right now the harness fails silently, and it fails without limits.

**Silently.** Nine sites swallow every exception:

```
src/lumberjack/core/supervisor.py:342,420,430,438,452
src/lumberjack/core/sensor.py:340,345
src/lumberjack/stand.py:148,151
```

Five of those are the background loops — oracle, train, conflicts, sync. If the oracle
raises on every probe, the stand keeps running, conflicts are never detected, agents
merrily produce work that will not merge, and *nothing anywhere says so*. That is the
worst failure mode this system has: it degrades into an expensive uncoordinated swarm
while appearing healthy.

**Without limits.** `Budget` declares `max_steps_per_task`, `max_wall_clock` and
`max_total_tokens`. Nothing reads them. A runaway loop runs until the operator notices.

## 2. Design

### 2.1 An error taxonomy

One module, `src/lumberjack/errors.py`, with a small hierarchy under a
`LumberjackError` base. `GitError` in `ports/git.py` moves under it (keep the old name
importable). The distinction that matters is **transient vs terminal**, because it is
the one the retry logic acts on:

```python
class LumberjackError(Exception):
    transient: bool = False
```

Transient: a git index lock, a model provider rate-limit or overload, a gate timeout.
Terminal: a protocol violation, an invalid ruling, a corrupt ledger row, a worktree that
has vanished from disk.

### 2.2 Stop swallowing; degrade loudly

Replace every `contextlib.suppress(Exception)` in the background loops with handling
that does three things: record the failure as an event, count consecutive failures, and
**stop the loop after a threshold rather than spinning forever**.

Add a `ComponentFailed` event (component, error, consecutive count, whether the loop is
giving up) so failures land in the ledger like everything else — which means `lj status`
and the dashboard can show them, and replay can explain them.

A degraded oracle must be visible in the awareness digest. An agent working with no
conflict detection deserves to know that is what is happening; it changes what a
reasonable agent would do.

`Supervisor._work`'s `except Exception` (line 342) blocks the task with
`str(error)[:500]` and discards the traceback. Keep the blocking behaviour — one agent
crashing must not stop the stand — but preserve the traceback in an artifact and
reference it from the event.

### 2.3 Retry what is worth retrying

No retry or backoff exists anywhere in the codebase today.

- **Git.** Index-lock contention is real when several worktrees share one object store.
  Retry with bounded exponential backoff and jitter, on a narrow allowlist of failures.
  Never retry a `merge-tree` that reported a genuine conflict — that is an answer, not
  an error.
- **Models.** Wrap the agent models in PydanticAI's `FallbackModel` so provider overload
  degrades to a second model instead of blocking a task. Make the fallback chain
  configurable on `StandConfig`.
- **The gate.** A timeout is transient once; twice is a hung test suite. Distinguish
  them.

### 2.4 Enforce the budget

**0002 is building `UsageLedger.totals()` and `for_workstream()`.** Consume them.

- `max_wall_clock` per task: the supervisor already records `started_at` per workstream.
- `max_steps_per_task`: cap the agent run, surface it as `BlockReason.BUDGET_EXHAUSTED`
  — the enum member already exists and is never used.
- `max_total_tokens`: stand-wide. On breach, halt cleanly: emit `StandHalted`, let
  in-flight work finish, and preserve every worktree.

If 0002's interface is not merged when you need it, agree the two signatures with that
agent and build against them rather than inventing a parallel accounting path.

### 2.5 Make the cleanup path trustworthy

`Stand.close()` (lines 148–151) catches bare `Exception` and treats any failure as
"preserve the worktree". The instinct is right — never destroy unlanded work — but the
operator is told nothing about *why* a worktree survived. Distinguish "kept because it
holds unlanded work" from "kept because removal failed", and report both.

### 2.6 Cancellation is a hazard, not a mechanism

0001_SPEC.md §19.3 records two bugs already found here: `watchfiles` sets the stop event it is
handed, and cancelling a loop mid-`git` leaks subprocess transports. `_shutdown` handles
the second by asking loops to leave at a checkpoint. Audit the rest of the codebase for
the same shape, and add a regression test for each — the `watchfiles` bug silently shut
down every other workstream and no test would have caught it.

### 2.7 Agent-facing errors are prompts

`ModelRetry` messages in `agents/toolsets.py` and `agents/workspace.py` are read by a
model deciding what to do next. Audit them: every one should say what went wrong *and*
what to do instead. `run_command`'s refusal of forbidden git subcommands is the standard
to match — it names the reason and points at `request_land`.

## 3. Acceptance criteria

- [ ] No `contextlib.suppress(Exception)` or bare `except Exception` remains outside
      `errors.py`, except where a comment justifies it.
- [ ] A background loop that fails repeatedly stops, emits `ComponentFailed`, and the
      degradation appears in the awareness digest.
- [ ] Git operations retry on a documented allowlist with bounded backoff; a real
      merge conflict is never retried.
- [ ] All three `Budget` limits are enforced, each with a test.
- [ ] `BlockReason.BUDGET_EXHAUSTED` is reachable.
- [ ] Model provider failure degrades through `FallbackModel` rather than blocking.
- [ ] `Stand.close()` distinguishes preserved-because-unlanded from
      preserved-because-cleanup-failed.
- [ ] `uv run ruff check .`, `uv run ty check`, `uv run pytest` all clean.

## 4. Out of scope

- Rendering failures. 0003 owns the display; emit the events and let it read them.
- Measuring. 0002 owns telemetry; use its numbers, do not duplicate the collection.
- Retrying a *task* end to end. The merge train already bounces with `bounce_limit`.

## 5. Tests

`tests/unit/test_errors.py` — the taxonomy, transient classification, backoff bounds.

`tests/integration/test_resilience.py` — a fault-injecting `GitBackend` and `Gate`
wrapping the real ones. Assert: an oracle that always raises stops its loop and emits
`ComponentFailed`; a git index lock succeeds on retry; a worker that raises blocks only
its own task while its peers land; a stand over its token budget halts and preserves
every worktree.

Fault injection is the missing capability in the current suite — the ports exist
precisely so failures can be injected at them. Build the fakes as reusable wrappers, not
one-off mocks; 0002 and 0003 will both want them.
