"""Run each workstream as a headless Claude Code session.

The reason this exists is money.  The in-process PydanticAI worker is billed per token
against an API key; a Claude Code session is billed against a flat-rate plan.  For a
swarm -- N agents, each running a long tool loop over a repository -- that difference is
the difference between a tool you use and a tool you demo once.

The session is not a client of this harness so much as a member of the swarm: it
attaches over MCP (``lj serve``) and claims, negotiates and lands under exactly the same
rules as the built-in worker.  Lumberjack orchestrates an agent it did not write, which
was always the point of the MCP surface.

Requires the ``claude`` CLI on ``PATH`` and an active session (``claude login``).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lumberjack.agents.outputs import TaskBlocked, TaskCompleted, WorkerOutput
from lumberjack.core.services import Services
from lumberjack.domain.events import NotePosted
from lumberjack.domain.note import Note
from lumberjack.domain.task import BlockReason, TaskSpec
from lumberjack.domain.workstream import Workstream
from lumberjack.ids import new_note_id

__all__ = ["ClaudeCodeRunner", "SessionResult", "render_brief"]

BRIEF = """\
You are `{agent}`, one of several agents working on this repository at the same time.

Your worktree is `{worktree}` on branch `{branch}`. It is yours alone -- no other agent
can edit it -- but every workstream merges into the same integration branch, so what
your peers are doing matters to you.

You have a `lumberjack` MCP server attached. **Call `join` with agent id `{agent}`
before anything else**; the other tools refuse to work until you do.

Then, in roughly this order:

1. `awareness()` -- your scoped digest: which peers overlap your files, what conflicts
   are open, unread messages, how far you have drifted from the integration branch.
   Re-read it when you change direction.
2. `claim(patterns=[...], mode="edit", rationale="...")` before you edit anything. Use
   `exclusive` only for renames, deletions, moves, mass reformatting or code generation
   -- things that defeat line-based merging. Two agents holding `edit` on one file is
   normal and expected: git merges different functions cleanly, and the claim exists so
   you learn about each other, not so you take turns.
3. `who_touches([...])` when you are about to work somewhere crowded, and `message(...)`
   the peer directly if the fix is really theirs or you are about to change something
   they depend on.
4. `post_note(...)` for decisions the next agent should not have to relitigate.
5. `check_merge()` before you finish. It runs a real git merge, not a guess.
6. `request_land()` when the task is done and verified.

TASK {task_id}: {title}

{intent}

Acceptance criteria:
{acceptance}

Verify your own work before landing -- run the project's checks:

    uv run ruff check . && uv run ty check && uv run pytest

Finish by stating plainly whether you completed the task or are blocked, and why.
"""


@dataclass(frozen=True, slots=True)
class SessionResult:
    """What a headless session reported back."""

    ok: bool
    text: str
    turns: int = 0
    cost_usd: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def render_brief(workstream: Workstream, spec: TaskSpec) -> str:
    acceptance = "\n".join(f"  - {item}" for item in spec.acceptance) or "  - (none given)"
    return BRIEF.format(
        agent=workstream.agent,
        worktree=workstream.worktree.path,
        branch=workstream.worktree.branch,
        task_id=spec.task_id,
        title=spec.title,
        intent=spec.intent,
        acceptance=acceptance,
    )


@dataclass(slots=True)
class ClaudeCodeRunner:
    """Spawns ``claude -p`` inside each worktree, attached to this stand over MCP."""

    repo: Path
    model: str = "opus"
    permission_mode: str = "acceptEdits"
    binary: str = field(default_factory=lambda: shutil.which("claude") or "claude")
    timeout_seconds: float = 3600.0
    extra_args: tuple[str, ...] = ()
    name: str = "claude_code"

    async def run(self, workstream: Workstream, spec: TaskSpec, services: Services) -> WorkerOutput:
        config = self._write_mcp_config(workstream, services)
        command = [
            self.binary,
            "-p",
            render_brief(workstream, spec),
            "--model",
            self.model,
            "--mcp-config",
            str(config),
            "--permission-mode",
            self.permission_mode,
            "--output-format",
            "json",
            "--add-dir",
            str(workstream.worktree.path),
            *self.extra_args,
        ]
        session = await self._spawn(command, cwd=workstream.worktree.path)
        await self._record(workstream, session, services)

        if not session.ok:
            return TaskBlocked(
                reason=BlockReason.AGENT_ERROR,
                needs=session.text[:2000] or "the claude session failed with no output",
            )
        return TaskCompleted(summary=session.text[:4000] or "session finished")

    # -- process ---------------------------------------------------------------------

    async def _spawn(self, command: list[str], *, cwd: Path) -> SessionResult:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd),
                env={**os.environ},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError) as error:
            return SessionResult(
                ok=False,
                text=(
                    f"could not start {command[0]!r}: {error}. Is the claude CLI on PATH, "
                    "and have you run `claude login`?"
                ),
            )
        try:
            raw_out, raw_err = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout_seconds
            )
        except TimeoutError:
            process.kill()
            return SessionResult(ok=False, text=f"session timed out after {self.timeout_seconds}s")

        out = raw_out.decode("utf-8", "replace")
        err = raw_err.decode("utf-8", "replace")
        if process.returncode:
            return SessionResult(ok=False, text=(err or out)[-4000:])
        return _parse(out, fallback=err)

    def _write_mcp_config(self, workstream: Workstream, services: Services) -> Path:
        """One config per worktree, pointing the session back at this stand."""
        target = workstream.worktree.path / ".lumberjack-mcp.json"
        payload = {
            "mcpServers": {
                "lumberjack": {
                    "command": "uv",
                    "args": [
                        "--directory",
                        str(self.repo.resolve()),
                        "run",
                        "lj",
                        "serve",
                        "--repo",
                        str(self.repo.resolve()),
                        "--stand",
                        str(services.stand),
                    ],
                }
            }
        }
        target.write_text(json.dumps(payload, indent=2))
        return target

    async def _record(
        self, workstream: Workstream, session: SessionResult, services: Services
    ) -> None:
        """Put the session's own account of itself on the blackboard.

        The ledger records what the session *did* through the MCP tools; this records
        what it said it did, which is what a human reading `lj board` actually wants.
        """
        cost = f" (~${session.cost_usd:.2f})" if session.cost_usd is not None else ""
        await services.ledger.append(
            NotePosted(
                note=Note(
                    note_id=new_note_id(),
                    author=workstream.agent,
                    topic="handoff",
                    body=f"[{session.turns} turns{cost}] {session.text[:1800]}",
                    posted_at=services.clock.now(),
                )
            ),
            actor=workstream.agent,
        )


def _parse(payload: str, *, fallback: str = "") -> SessionResult:
    """``claude -p --output-format json`` emits one result object; be forgiving anyway."""
    text = payload.strip()
    if not text:
        return SessionResult(ok=False, text=fallback[-4000:] or "no output")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return SessionResult(ok=True, text=text[-4000:])
    if not isinstance(parsed, dict):
        return SessionResult(ok=True, text=text[-4000:])

    result = parsed.get("result")
    return SessionResult(
        ok=not parsed.get("is_error", False),
        text=str(result) if result is not None else text[-4000:],
        turns=int(parsed.get("num_turns", 0) or 0),
        cost_usd=parsed.get("total_cost_usd"),
        raw=parsed,
    )
