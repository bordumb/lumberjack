"""``lj`` end to end, against a ledger on disk.

Commands are invoked the way a shell invokes them -- through the cyclopts app, with
argument strings -- so what these assert on is exactly what an operator gets: the text,
the JSON, and the exit code.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from lumberjack.adapters.sqlite_ledger import SqliteLedger
from lumberjack.cli import live
from lumberjack.cli.main import app
from lumberjack.cli.output import Output, set_default_output
from lumberjack.cli.render import ExitCode
from lumberjack.ids import StandId


@pytest.fixture
def shown() -> Iterator[StringIO]:
    """Everything the command line writes, with the real console swapped out."""
    buffer = StringIO()
    set_default_output(Output.to(buffer, width=200))
    yield buffer
    set_default_output(None)


@pytest.fixture
def invoke(shown: StringIO) -> Callable[..., ExitCode]:
    def call(*tokens: str) -> ExitCode:
        try:
            app(list(tokens))
        except SystemExit as leaving:
            return ExitCode(int(leaving.code or 0))
        return ExitCode.OK

    return call


@pytest.fixture
def empty_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "empty"
    repo.mkdir()
    return repo


# -- exit codes ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        ("status",),
        ("conflicts",),
        ("board",),
        ("comments",),
        ("replay", "stand-nope"),
        ("halt",),
        ("rename", "a name"),
    ],
)
def test_a_command_with_no_stand_exits_three_and_says_what_to_do(
    invoke, shown, empty_repo, command
):
    """Every one of these used to print a bare string and exit zero."""
    code = invoke(*command, "--repo", str(empty_repo))

    assert code is ExitCode.NO_STAND
    assert "lj run" in shown.getvalue(), "a dead end has to name the way out"


def test_naming_a_stand_that_does_not_exist_is_the_same_failure(invoke, recorded_stand):
    repo, _ = recorded_stand

    assert invoke("status", "--repo", str(repo), "--stand", "stand-ghost") is ExitCode.NO_STAND


def test_a_run_with_neither_goal_nor_spec_is_a_usage_error(invoke, shown, empty_repo):
    code = invoke("run", "--repo", str(empty_repo))

    assert code is ExitCode.USAGE
    assert "give a goal" in shown.getvalue()


def test_explaining_a_conflict_that_is_not_open_is_a_usage_error(invoke, recorded_stand):
    repo, _ = recorded_stand

    assert invoke("conflicts", "--repo", str(repo), "--explain", "cfl-ghost") is ExitCode.USAGE


def test_reading_a_recorded_stand_succeeds(invoke, recorded_stand):
    repo, _ = recorded_stand

    assert invoke("status", "--repo", str(repo)) is ExitCode.OK


# -- what the commands actually say ----------------------------------------------------


def test_status_leads_with_the_lifecycle_and_the_landings(invoke, shown, recorded_stand):
    repo, stand = recorded_stand

    invoke("status", "--repo", str(repo))

    body = shown.getvalue()
    assert stand in body
    assert "1 task(s) landed" in body
    assert "agent-task-render" in body and "agent-task-watch" in body


def test_halting_a_stand_changes_what_status_leads_with(invoke, shown, recorded_stand):
    """A halted stand rendering identically to a working one is what misled people."""
    repo, _ = recorded_stand
    invoke("halt", "--repo", str(repo), "--reason", "operator halt")
    shown.truncate(0)
    shown.seek(0)

    invoke("status", "--repo", str(repo))

    body = shown.getvalue()
    assert "HALTED" in body
    assert "nothing below is running" in body


def test_conflicts_explain_shows_the_evidence_and_the_transcript(invoke, shown, recorded_stand):
    repo, _ = recorded_stand

    invoke("conflicts", "--repo", str(repo), "--explain", "cfl-recorded")

    body = shown.getvalue()
    assert "def alpha() -> str:" in body
    assert "I will take the signature" in body


def test_the_board_prints_the_notes(invoke, shown, recorded_stand):
    repo, _ = recorded_stand

    invoke("board", "--repo", str(repo))

    assert "alpha returns str from now on" in shown.getvalue()


def test_comments_no_longer_shows_resolved_ones_by_accident(invoke, shown, recorded_stand):
    """``if all or not item.resolved`` was always true: ``all`` is the builtin."""
    repo, _ = recorded_stand

    invoke("comments", "--repo", str(repo))

    assert "no review comments" in shown.getvalue()


# -- --json ----------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["status", "conflicts", "board"])
def test_json_output_parses(invoke, shown, recorded_stand, command):
    repo, _ = recorded_stand

    code = invoke(command, "--repo", str(repo), "--json")

    assert code is ExitCode.OK
    json.loads(shown.getvalue())


def test_replay_json_decodes_every_event(invoke, shown, recorded_stand):
    repo, stand = recorded_stand

    invoke("replay", stand, "--repo", str(repo), "--json")

    decoder = json.JSONDecoder()
    body, seen = shown.getvalue().strip(), 0
    index = 0
    while index < len(body):
        _document, offset = decoder.raw_decode(body, index)
        seen += 1
        index = offset
        while index < len(body) and body[index] in " \n\r\t":
            index += 1
    assert seen > 5, "every recorded event should decode"


def test_json_never_truncates_an_identifier(invoke, shown, recorded_stand):
    repo, _ = recorded_stand

    invoke("status", "--repo", str(repo), "--json")

    document = json.loads(shown.getvalue())
    assert document["conflicts"][0]["conflict"] == "cfl-recorded"


# -- lj init ---------------------------------------------------------------------------


def gate_of(repo: Path) -> list[list[str]]:
    return json.loads((repo / "lumberjack.json").read_text())["gate_commands"]


def test_init_writes_the_python_gate_for_a_python_project(invoke, tmp_path):
    repo = tmp_path / "py"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n")

    invoke("init", str(repo))

    assert ["uv", "run", "pytest", "-q"] in gate_of(repo)


def test_init_writes_the_declared_scripts_for_a_node_project(invoke, tmp_path):
    repo = tmp_path / "node"
    repo.mkdir()
    scripts = {"scripts": {"lint": "eslint", "test": "vitest"}}
    (repo / "package.json").write_text(json.dumps(scripts))

    invoke("init", str(repo))

    assert gate_of(repo) == [["npm", "run", "lint"], ["npm", "run", "test"]]


def test_init_leaves_the_gate_empty_when_it_cannot_tell(invoke, shown, tmp_path):
    """A gate of the wrong language teaches the agents on the first bounce to ignore it."""
    repo = tmp_path / "mystery"
    repo.mkdir()

    invoke("init", str(repo))

    assert gate_of(repo) == []
    assert "gate_commands" in shown.getvalue(), "say what to fill in"


def test_plan_no_longer_takes_a_dry_run_flag(invoke, tmp_path):
    """It defaulted to true and changed nothing but a closing hint."""
    repo = tmp_path / "py"
    repo.mkdir()

    assert invoke("plan", "a goal", "--repo", str(repo), "--dry-run") is not ExitCode.OK


# -- the live view ---------------------------------------------------------------------


async def until(predicate: Callable[[], bool], *, limit: float = 10.0) -> None:
    waited = 0.0
    while not predicate() and waited < limit:
        await asyncio.sleep(0.05)
        waited += 0.05


async def follow_recorded(
    repo: Path, stand: StandId, output: Output, buffer: StringIO, marker: str
) -> None:
    """Follow a recorded ledger until it has rendered ``marker``, then stop.

    Cancellation is how a real run ends the view too: the supervisor returns and
    ``lj run`` cancels the follower.
    """
    ledger = await SqliteLedger.open(stand, repo / ".lumberjack" / stand / "ledger.db")
    follower = asyncio.create_task(live.follow(ledger, stand=stand, output=output))
    try:
        await until(lambda: marker in buffer.getvalue())
    finally:
        follower.cancel()
        await asyncio.gather(follower, return_exceptions=True)
        await ledger.close()


async def test_a_non_tty_run_degrades_to_one_line_per_event(recorded_stand):
    """``lj run > log.txt`` and CI both have to stay sane."""
    repo, stand = recorded_stand
    buffer = StringIO()
    output = Output.to(buffer, width=200)

    await follow_recorded(repo, stand, output, buffer, marker="alpha returns str")

    lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
    assert all(line.startswith("1") for line in lines), "each line opens with its timestamp"
    assert any("stand stand-recorded on integration/stand-recorded" in line for line in lines)
    assert any("claims pkg/core.py (edit) alongside" in line for line in lines)
    assert not any("worktree_delta" in line for line in lines), "counted, not fed"


async def test_a_terminal_gets_the_per_workstream_lanes(recorded_stand):
    """On a TTY the same events become a frame: one lane per workstream, plus the feed."""
    repo, stand = recorded_stand
    buffer = StringIO()
    console = Console(file=buffer, force_terminal=True, width=120)
    terminal = Output(stdout=console, stderr=console)
    assert terminal.rich, "force_terminal is what makes this the live path"

    await follow_recorded(repo, stand, terminal, buffer, marker="agent-task-watch")

    body = buffer.getvalue()
    assert "agent-task-render" in body, "a lane per workstream"
    assert "events" in body, "and the feed beside it"
