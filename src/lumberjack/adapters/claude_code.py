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
import contextlib
import json
import os
import shutil
import signal
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

__all__ = [
    "COORDINATION_TOOLS",
    "ClaudeCodeRunner",
    "CoordinationUnavailableError",
    "SessionResult",
    "render_brief",
]

COORDINATION_TOOLS = "mcp__lumberjack"
"""Always allowed.  A session that cannot call these is not in the swarm."""

VERIFICATION_TOOLS = ("Bash", "Read", "Write", "Edit", "Glob", "Grep")
"""Also always allowed.

Every spec asks the agent to run ruff, ty and pytest before landing, and the first run
could not: `uv run pytest` came back "This command requires approval" in a session with
nobody to approve it. An agent that cannot check its own work reports an unverified
branch as finished, which is worse than reporting a red one."""

_NESTING_VARS = ("CLAUDECODE", "CLAUDE_CODE_SSE_PORT", "CLAUDE_CODE_ENTRYPOINT")

_DENIAL_MARKER = "requested permissions to use"


class CoordinationUnavailableError(RuntimeError):
    """The infrastructure a swarm depends on is missing or refusing.

    Raised loudly and early.  A stand that runs without coordination still burns
    tokens and still writes code -- it just writes it blind, and the harness reports
    healthy progress the whole time.  That is worse than not starting.
    """


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
    extra_allowed_tools: tuple[str, ...] = ()
    binary: str = field(default_factory=lambda: shutil.which("claude") or "claude")
    timeout_seconds: float = 3600.0
    probe_timeout: float = 120.0
    extra_args: tuple[str, ...] = ()
    name: str = "claude_code"

    @property
    def allowed_tools(self) -> str:
        """Coordination and self-verification are never opt-out."""
        return " ".join((COORDINATION_TOOLS, *VERIFICATION_TOOLS, *self.extra_allowed_tools))

    async def preflight(self, services: Services) -> None:
        """Check every piece of coordination infrastructure before spawning anything."""
        if not shutil.which(self.binary) and not Path(self.binary).is_file():
            msg = (
                f"the claude CLI was not found at {self.binary!r}. Install it and run "
                "`claude login`, or choose --runtime pydantic_ai."
            )
            raise CoordinationUnavailableError(msg)
        if not shutil.which("uv"):
            msg = "uv is not on PATH; the MCP server runs as `uv run lj serve`."
            raise CoordinationUnavailableError(msg)

        await self._check_login()

        ledger = services.config.resolved_state_root() / str(services.stand) / "ledger.db"
        if not ledger.is_file():
            msg = (
                f"no ledger at {ledger}. Sessions attach to a stand through it, so it "
                "must exist before any of them start."
            )
            raise CoordinationUnavailableError(msg)
        await self._probe_server(services)

    async def _check_login(self) -> None:
        """Ask the CLI to do the smallest possible thing.

        Preflight checked that the binary existed, which it did -- and every session
        then died on an expired OAuth token after three worktrees had been created. A
        credential is infrastructure like any other, so it is checked before the work
        starts rather than discovered by each agent separately.
        """
        try:
            process = await asyncio.create_subprocess_exec(
                self.binary,
                "-p",
                "Reply with the single word: ok",
                "--output-format",
                "json",
                cwd=str(self.repo.resolve()),
                env=_child_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            raw, err = await asyncio.wait_for(process.communicate(), timeout=self.probe_timeout)
        except (OSError, ValueError, TimeoutError) as error:
            msg = f"could not reach the claude CLI: {error}"
            raise CoordinationUnavailableError(msg) from error

        answer = _parse(raw.decode("utf-8", "replace"), fallback=err.decode("utf-8", "replace"))
        if answer.ok:
            return
        remedy = (
            " Run `claude login` and try again."
            if "auth" in answer.text.lower() or "oauth" in answer.text.lower()
            else ""
        )
        msg = f"the claude CLI could not run a request: {answer.text.strip()[:300]}{remedy}"
        raise CoordinationUnavailableError(msg)

    async def _probe_server(self, services: Services) -> None:
        """Speak MCP to our own server before trusting sessions to it.

        A config pointing at a broken command produces sessions that look busy and
        coordinate with nothing, which is the failure this whole check exists to stop.
        """
        handshake = "\n".join(
            json.dumps(message)
            for message in (
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "lumberjack-preflight", "version": "1"},
                    },
                },
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            )
        )
        process = await asyncio.create_subprocess_exec(
            "uv",
            "run",
            "lj",
            "serve",
            "--repo",
            str(self.repo.resolve()),
            "--stand",
            str(services.stand),
            cwd=str(self.repo.resolve()),
            env=_child_env(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            if process.stdin is not None:
                process.stdin.write(f"{handshake}\n".encode())
                await process.stdin.drain()
            answered = await asyncio.wait_for(
                _read_until(process, needle='"tools"'), timeout=self.probe_timeout
            )
        except TimeoutError:
            answered = False
        finally:
            # An MCP stdio server serves until its input closes, so this never exits on
            # its own. Waiting for it to is how the check used to fail against a server
            # that was answering perfectly well.
            await _terminate(process)

        if not answered:
            detail = ""
            if process.stderr is not None:
                with contextlib.suppress(Exception):
                    detail = (await process.stderr.read())[-600:].decode("utf-8", "replace")
            msg = (
                "the lumberjack MCP server did not list its tools within "
                f"{self.probe_timeout:.0f}s" + (f":\n{detail}" if detail.strip() else "")
            )
            raise CoordinationUnavailableError(msg)

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
            # `acceptEdits` covers file edits but still prompts for MCP tools, and a
            # headless session has nobody to prompt: every coordination call is denied
            # and the agent works blind while the harness assumes it is participating.
            "--allowedTools",
            self.allowed_tools,
            "--output-format",
            "json",
            "--add-dir",
            str(workstream.worktree.path.resolve()),
            *self.extra_args,
        ]
        session = await self._spawn(command, cwd=workstream.worktree.path)
        await self._record(workstream, session, services)

        if not session.ok:
            return TaskBlocked(
                reason=BlockReason.AGENT_ERROR,
                needs=session.text[:2000] or "the claude session failed with no output",
            )

        calls, denials = _coordination_report(workstream.worktree.path)
        if denials:
            return TaskBlocked(
                reason=BlockReason.AGENT_ERROR,
                needs=(
                    f"the session was refused {denials} coordination call(s). It ran "
                    "without claims, awareness or conflict checks -- the work it produced "
                    "was written blind. Check that --allowedTools includes "
                    f"{COORDINATION_TOOLS!r}."
                ),
            )
        if calls == 0:
            return TaskBlocked(
                reason=BlockReason.AGENT_ERROR,
                needs=(
                    "the session never called a coordination tool, so it was not part of "
                    "the swarm. Check that the MCP server started: `lj serve --stand "
                    f"{services.stand}`."
                ),
            )
        return TaskCompleted(summary=session.text[:4000] or "session finished")

    # -- process ---------------------------------------------------------------------

    async def _spawn(self, command: list[str], *, cwd: Path) -> SessionResult:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd),
                env=_child_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # Its own process group, so stopping a workstream stops everything the
                # session started -- a test run, a build, a dev server. Signalling only
                # the parent leaves those orphaned inside a worktree nobody is watching.
                start_new_session=True,
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
            await _terminate(process)
            return SessionResult(ok=False, text=f"session timed out after {self.timeout_seconds}s")
        except asyncio.CancelledError:
            # `lj halt` and Ctrl-C cancel the task awaiting this child. Without this,
            # the cancellation unwinds and leaves a detached `claude` session still
            # editing a worktree that the harness has stopped watching -- the one
            # failure mode where stopping the stand makes things worse than leaving
            # it running.
            await _terminate(process)
            raise

        out = raw_out.decode("utf-8", "replace")
        err = raw_err.decode("utf-8", "replace")
        if process.returncode:
            # A failed run still answers in JSON, and that answer says why. Returning
            # the raw stream instead put a page of escaped JSON on the blackboard where
            # the sentence "OAuth session expired" was the only part that mattered.
            parsed = _parse(out, fallback=err)
            return SessionResult(
                ok=False,
                text=parsed.text or (err or out)[-4000:],
                turns=parsed.turns,
                raw=parsed.raw,
            )
        return _parse(out, fallback=err)

    def _write_mcp_config(self, workstream: Workstream, services: Services) -> Path:
        """One config per workstream, pointing the session back at this stand.

        Written to the stand's state directory rather than into the worktree: anything
        left in a worktree is picked up by ``git add -A`` when the work is committed,
        and a harness that lands its own scaffolding onto the integration branch is
        not one anybody should trust.  The path is absolute because the session runs
        with its cwd set to the worktree.
        """
        folder = services.config.resolved_state_root() / str(services.stand) / "mcp"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{workstream.workstream_id}.json"
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
        return target.resolve()

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


async def _read_until(process: asyncio.subprocess.Process, *, needle: str) -> bool:
    """Read stdout until the needle appears.  Returns False if the stream ends first."""
    if process.stdout is None:
        return False
    seen = ""
    while True:
        chunk = await process.stdout.read(4096)
        if not chunk:
            return False
        seen += chunk.decode("utf-8", "replace")
        if needle in seen:
            return True


async def _terminate(process: asyncio.subprocess.Process, grace: float = 5.0) -> None:
    """SIGTERM the whole group, then SIGKILL it.

    A session mid-edit deserves the chance to finish a write, which is why the first
    signal is TERM. The group rather than the process because the session spawns its
    own children and they hold the worktree open.
    """
    if process.returncode is not None:
        return
    _signal_group(process, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=grace)
    except TimeoutError:
        _signal_group(process, signal.SIGKILL)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=grace)
    # A cancelled `communicate()` leaves the pipes open. Draining closes the transport,
    # but it is bounded: a grandchild still holding stdout must not stall a shutdown.
    with contextlib.suppress(Exception):
        await asyncio.wait_for(process.communicate(), timeout=1.0)


def _signal_group(process: asyncio.subprocess.Process, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(os.getpgid(process.pid), sig)


def _child_env() -> dict[str, str]:
    """The nesting guard has to go.

    ``claude`` refuses to start inside another session because nested sessions share
    runtime resources. Ours are not nested in that sense -- each is a separate process
    in its own worktree -- but the guard is an environment variable, so a stand started
    from inside a Claude Code session dies three times over with a message nobody reads.
    """
    env = {key: value for key, value in os.environ.items() if key not in _NESTING_VARS}
    return env


def _coordination_report(worktree: Path) -> tuple[int, int]:
    """(coordination calls made, permission denials) from the session's own transcript."""
    folder = (
        Path.home() / ".claude" / "projects" / str(worktree).replace("/", "-").replace(".", "-")
    )
    if not folder.is_dir():
        return (0, 0)
    logs = sorted(folder.glob("*.jsonl"), key=lambda item: item.stat().st_mtime)
    if not logs:
        return (0, 0)

    calls = denials = 0
    for line in logs[-1].read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        content = (event.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and str(block.get("name", "")).startswith(
                COORDINATION_TOOLS
            ):
                calls += 1
            elif block.get("type") == "tool_result":
                rendered = json.dumps(block.get("content", ""))
                if _DENIAL_MARKER in rendered and COORDINATION_TOOLS in rendered:
                    denials += 1
    return (calls, denials)


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
