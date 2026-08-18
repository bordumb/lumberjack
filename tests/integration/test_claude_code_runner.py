"""The Claude Code runner: a headless session as a swarm member.

The tests stub the ``claude`` binary with a script, because what needs verifying is the
contract with the CLI -- the flags, the working directory, the MCP config -- not
Anthropic's model.
"""

from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path

import pytest

from lumberjack.adapters.claude_code import ClaudeCodeRunner, _parse, render_brief
from lumberjack.agents.outputs import TaskBlocked, TaskCompleted
from lumberjack.core.services import Services


def fake_claude(tmp_path: Path, *, body: str) -> Path:
    """A stand-in for the CLI that records its argv and prints a scripted result."""
    script = tmp_path / "claude"
    script.write_text(f'#!/bin/sh\nprintf \'%s\\n\' "$@" > "$0.argv"\npwd > "$0.cwd"\n{body}\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


async def still_running(pattern: str | Path) -> bool:
    await asyncio.sleep(0.3)
    process = await asyncio.create_subprocess_exec(
        "pgrep",
        "-f",
        str(pattern),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return await process.wait() == 0


def argv_of(script: Path) -> list[str]:
    return Path(f"{script}.argv").read_text().splitlines()


async def test_a_successful_session_completes_the_task(
    services: Services, make_workstream, tmp_path: Path
) -> None:
    payload = json.dumps(
        {
            "type": "result",
            "is_error": False,
            "result": "raised alpha to 111",
            "num_turns": 7,
            "total_cost_usd": 0.0,
        }
    )
    script = fake_claude(tmp_path, body=f"echo '{payload}'")
    workstream = await make_workstream("a")
    spec = services.projections.specs[workstream.task]

    output = await ClaudeCodeRunner(repo=services.config.repo, binary=str(script)).run(
        workstream, spec, services
    )

    assert isinstance(output, TaskCompleted)
    assert "raised alpha" in output.summary


async def test_the_session_runs_inside_the_worktree(
    services: Services, make_workstream, tmp_path: Path
) -> None:
    """A session that runs in the main repo would edit everyone's work at once."""
    script = fake_claude(tmp_path, body='echo \'{"result": "ok"}\'')
    workstream = await make_workstream("a")
    spec = services.projections.specs[workstream.task]

    await ClaudeCodeRunner(repo=services.config.repo, binary=str(script)).run(
        workstream, spec, services
    )

    cwd = Path(f"{script}.cwd").read_text().strip()
    assert Path(cwd).resolve() == workstream.worktree.path.resolve()


async def test_the_flags_match_the_cli_contract(
    services: Services, make_workstream, tmp_path: Path
) -> None:
    script = fake_claude(tmp_path, body='echo \'{"result": "ok"}\'')
    workstream = await make_workstream("a")
    spec = services.projections.specs[workstream.task]

    await ClaudeCodeRunner(repo=services.config.repo, binary=str(script), model="opus").run(
        workstream, spec, services
    )

    argv = argv_of(script)
    assert "-p" in argv
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--add-dir") + 1] == str(workstream.worktree.path)
    # Without this the session is denied every coordination call and works blind.
    assert argv[argv.index("--allowedTools") + 1] == "mcp__lumberjack"


async def test_an_mcp_config_is_written_pointing_at_this_stand(
    services: Services, make_workstream, tmp_path: Path
) -> None:
    """This is how the session joins the swarm rather than working blind."""
    script = fake_claude(tmp_path, body='echo \'{"result": "ok"}\'')
    workstream = await make_workstream("a")
    spec = services.projections.specs[workstream.task]

    await ClaudeCodeRunner(repo=services.config.repo, binary=str(script)).run(
        workstream, spec, services
    )

    argv = argv_of(script)
    written = Path(argv[argv.index("--mcp-config") + 1])
    config = json.loads(written.read_text())
    args = config["mcpServers"]["lumberjack"]["args"]
    assert "serve" in args
    assert args[args.index("--stand") + 1] == services.stand


async def test_a_failing_session_blocks_only_its_own_task(
    services: Services, make_workstream, tmp_path: Path
) -> None:
    script = fake_claude(tmp_path, body="echo 'boom' >&2; exit 1")
    workstream = await make_workstream("a")
    spec = services.projections.specs[workstream.task]

    output = await ClaudeCodeRunner(repo=services.config.repo, binary=str(script)).run(
        workstream, spec, services
    )

    assert isinstance(output, TaskBlocked)
    assert "boom" in output.needs


async def test_a_missing_binary_explains_itself(services: Services, make_workstream) -> None:
    workstream = await make_workstream("a")
    spec = services.projections.specs[workstream.task]

    output = await ClaudeCodeRunner(repo=services.config.repo, binary="/nonexistent/claude").run(
        workstream, spec, services
    )

    assert isinstance(output, TaskBlocked)
    assert "claude login" in output.needs


async def test_the_session_reports_back_on_the_blackboard(
    services: Services, make_workstream, tmp_path: Path
) -> None:
    payload = json.dumps({"result": "did the thing", "num_turns": 3, "is_error": False})
    script = fake_claude(tmp_path, body=f"echo '{payload}'")
    workstream = await make_workstream("a")
    spec = services.projections.specs[workstream.task]

    await ClaudeCodeRunner(repo=services.config.repo, binary=str(script)).run(
        workstream, spec, services
    )

    notes = services.projections.notes
    assert notes
    assert "did the thing" in notes[-1].body
    assert "3 turns" in notes[-1].body


def test_the_brief_tells_the_session_to_join_first(services: Services) -> None:
    """Every other MCP tool refuses until it does, so this must be unmissable."""
    from lumberjack.domain.task import TaskSpec
    from lumberjack.domain.workstream import Workstream, Worktree
    from lumberjack.ids import AgentId, CommitSha, TaskId, WorkstreamId

    workstream = Workstream(
        workstream_id=WorkstreamId("ws-a"),
        stand=services.stand,
        agent=AgentId("agent-a"),
        task=TaskId("t"),
        worktree=Worktree(path=Path("/tmp/wt"), branch="lj/a", base=CommitSha("0" * 40)),
    )
    brief = render_brief(
        workstream,
        TaskSpec(task_id=TaskId("t"), title="do it", intent="the intent", acceptance=("x",)),
    )

    assert "join" in brief
    assert "agent-a" in brief
    assert "claim(" in brief
    assert "request_land()" in brief


@pytest.mark.parametrize(
    ("payload", "ok", "contains"),
    [
        ('{"is_error": false, "result": "fine"}', True, "fine"),
        ('{"is_error": true, "result": "nope"}', False, "nope"),
        ("not json at all", True, "not json"),
        ("", False, "no output"),
    ],
)
def test_parse_is_forgiving(payload: str, ok: bool, contains: str) -> None:
    result = _parse(payload)

    assert result.ok is ok
    assert contains in result.text


async def test_the_config_path_is_absolute_and_outside_the_worktree(
    services: Services, make_workstream, tmp_path: Path
) -> None:
    """Two bugs in one assertion.

    A relative ``--mcp-config`` is resolved against the session's cwd -- the worktree --
    so the path doubles and every session dies before it starts. And anything left
    inside a worktree is swept up by ``git add -A`` when the work is committed, which
    would land the harness's own scaffolding on the integration branch.
    """
    script = fake_claude(tmp_path, body='echo \'{"result": "ok"}\'')
    workstream = await make_workstream("a")
    spec = services.projections.specs[workstream.task]

    await ClaudeCodeRunner(repo=services.config.repo, binary=str(script)).run(
        workstream, spec, services
    )

    argv = argv_of(script)
    written = Path(argv[argv.index("--mcp-config") + 1])
    assert written.is_absolute()
    assert written.is_file()
    assert not written.is_relative_to(workstream.worktree.path.resolve())
    assert not list(workstream.worktree.path.glob("*mcp*.json"))


async def test_the_worktree_stays_clean_for_the_agent(
    services: Services, make_workstream, tmp_path: Path
) -> None:
    """The session must open onto its task, not onto the harness's leftovers."""
    script = fake_claude(tmp_path, body='echo \'{"result": "ok"}\'')
    workstream = await make_workstream("a")
    spec = services.projections.specs[workstream.task]

    await ClaudeCodeRunner(repo=services.config.repo, binary=str(script)).run(
        workstream, spec, services
    )

    assert not (await services.git.status(workstream.worktree)).dirty


async def test_cancelling_the_task_kills_the_session(
    services: Services, make_workstream, tmp_path: Path
) -> None:
    """Otherwise `lj halt` leaves a detached agent still editing a worktree.

    That is the one failure mode where stopping the stand is worse than leaving it
    running: the harness stops watching, and the session keeps writing.
    """
    # A distinctive duration so the grandchild is identifiable among all sleeps on
    # the machine: killing the session but orphaning what it spawned is the bug.
    script = fake_claude(tmp_path, body="sleep 3607")
    workstream = await make_workstream("a")
    spec = services.projections.specs[workstream.task]

    runner = ClaudeCodeRunner(repo=services.config.repo, binary=str(script))
    task = asyncio.create_task(runner.run(workstream, spec, services))
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not await still_running(script)


async def test_a_timed_out_session_is_killed_too(
    services: Services, make_workstream, tmp_path: Path
) -> None:
    script = fake_claude(tmp_path, body="sleep 3608")
    workstream = await make_workstream("a")
    spec = services.projections.specs[workstream.task]

    output = await ClaudeCodeRunner(
        repo=services.config.repo, binary=str(script), timeout_seconds=0.5
    ).run(workstream, spec, services)

    assert isinstance(output, TaskBlocked)
    assert "timed out" in output.needs
    assert not await still_running(script)
    assert not await still_running("sleep 3608")
