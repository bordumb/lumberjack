"""The Claude Code runner: a headless session as a swarm member.

The tests stub the ``claude`` binary with a script, because what needs verifying is the
contract with the CLI -- the flags, the working directory, the MCP config -- not
Anthropic's model.
"""

from __future__ import annotations

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

    config = json.loads((workstream.worktree.path / ".lumberjack-mcp.json").read_text())
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
