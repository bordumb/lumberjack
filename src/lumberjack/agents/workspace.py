"""The workspace toolset: how a worker actually changes code.

Worktrees share one object store, so a stray ``git push`` or ``gc`` in one workstream
damages every other.  Shell access is therefore allow-listed rather than open, and the
harness owns branch and ref manipulation entirely.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.toolsets import FunctionToolset

from lumberjack.agents.deps import WorkerDeps
from lumberjack.domain.glob import matches

__all__ = ["FORBIDDEN_GIT", "CommandResult", "workspace_toolset"]

FORBIDDEN_GIT: frozenset[str] = frozenset(
    {
        "push",
        "gc",
        "prune",
        "reflog",
        "worktree",
        "remote",
        "clone",
        "fetch",
        "pull",
        "filter-branch",
        "update-ref",
        "symbolic-ref",
    }
)

_MAX_READ = 200_000


class CommandResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: int
    output: str


workspace_toolset = FunctionToolset[WorkerDeps]()


def _resolve(ctx: RunContext[WorkerDeps], path: str) -> Path:
    root = ctx.deps.worktree.path.resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise ModelRetry(
            f"{path!r} is outside your worktree and was not touched. Paths are relative "
            f"to your own worktree root ({root}); if you need a change somewhere another "
            "agent owns, message them instead."
        )
    relative = target.relative_to(root).as_posix()
    for guard in ctx.deps.services.config.protected_paths:
        if matches(guard, relative):
            raise ModelRetry(
                f"{relative} is managed by the harness and was not touched. Coordination "
                "state is not yours to edit; use the coordination tools (claim, message, "
                "request_land) to get the same effect."
            )
    return target


@workspace_toolset.tool
async def read_file(ctx: RunContext[WorkerDeps], path: str) -> str:
    """Read a file from your worktree."""
    target = _resolve(ctx, path)
    if not target.is_file():
        raise ModelRetry(
            f"{path} does not exist in your worktree. Use list_dir to see what is there, "
            "or search(pattern) to find it by content."
        )
    return target.read_text(encoding="utf-8", errors="replace")[:_MAX_READ]


@workspace_toolset.tool
async def write_file(ctx: RunContext[WorkerDeps], path: str, content: str) -> str:
    """Write a file in your worktree.  Claim the path first."""
    target = _resolve(ctx, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.exists()
    target.write_text(content, encoding="utf-8")
    return f"{'updated' if existed else 'created'} {path} ({len(content)} bytes)"


@workspace_toolset.tool
async def list_dir(ctx: RunContext[WorkerDeps], path: str = ".") -> tuple[str, ...]:
    """List a directory in your worktree."""
    target = _resolve(ctx, path) if path not in ("", ".") else ctx.deps.worktree.path
    if not target.is_dir():
        raise ModelRetry(
            f"{path} is not a directory. Pass a directory, or read_file({path!r}) if you "
            "meant to read it."
        )
    return tuple(
        sorted(
            f"{item.name}/" if item.is_dir() else item.name
            for item in target.iterdir()
            if item.name != ".git"
        )
    )


@workspace_toolset.tool
async def search(ctx: RunContext[WorkerDeps], pattern: str, glob: str = "**/*") -> tuple[str, ...]:
    """Grep your worktree.  Returns ``path:line: text`` for the first 60 matches."""
    root = ctx.deps.worktree.path
    hits: list[str] = []
    for candidate in sorted(root.glob(glob)):
        if not candidate.is_file() or ".git" in candidate.parts:
            continue
        try:
            text = candidate.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if pattern in line:
                hits.append(f"{candidate.relative_to(root).as_posix()}:{number}: {line.strip()}")
                if len(hits) >= 60:
                    return tuple(hits)
    return tuple(hits)


@workspace_toolset.tool
async def run_command(
    ctx: RunContext[WorkerDeps], command: list[str], timeout_seconds: int = 300
) -> CommandResult:
    """Run a command in your worktree.

    Git subcommands that touch shared state (``push``, ``gc``, ``worktree``,
    ``update-ref`` and friends) are refused: every worktree shares one object store,
    and the harness owns refs.  Use ``request_land`` to integrate your work.
    """
    if not command:
        raise ModelRetry(
            "command must not be empty and nothing ran. Pass argv as a list, e.g. "
            "['uv', 'run', 'pytest', '-q']."
        )
    if Path(command[0]).name == "git" and len(command) > 1:
        subcommand = next((part for part in command[1:] if not part.startswith("-")), "")
        if subcommand in FORBIDDEN_GIT:
            raise ModelRetry(
                f"git {subcommand} is not available to agents: it affects every worktree "
                "in the stand. Use request_land to integrate your work."
            )
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(ctx.deps.worktree.path),
            env={**os.environ},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        raw, _ = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except TimeoutError:
        return CommandResult(exit_code=124, output=f"timed out after {timeout_seconds}s")
    except (OSError, ValueError) as error:
        raise ModelRetry(
            f"could not run {command[0]!r}: {error}. Nothing ran. Check the binary exists "
            "in this worktree, and reach project tools through `uv run <tool>`."
        ) from error
    return CommandResult(
        exit_code=process.returncode or 0,
        output=raw.decode("utf-8", "replace")[-20_000:],
    )
