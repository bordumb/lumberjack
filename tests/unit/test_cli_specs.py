"""``lj run --spec``: one agent per specification, no planner involved."""

from __future__ import annotations

from pathlib import Path

import pytest

from lumberjack.cli.main import _plan_from_specs


@pytest.fixture
def specs(tmp_path: Path) -> Path:
    folder = tmp_path / "docs" / "specs"
    folder.mkdir(parents=True)
    for name in ("0002_telemetry.md", "0003_ux.md", "0004_errors.md"):
        (folder / name).write_text(f"# {name}\n")
    return tmp_path


def test_one_task_per_spec_file(specs: Path) -> None:
    plan = _plan_from_specs(
        specs,
        [
            Path("docs/specs/0002_telemetry.md"),
            Path("docs/specs/0003_ux.md"),
            Path("docs/specs/0004_errors.md"),
        ],
    )

    assert [item.task_id for item in plan.tasks] == [
        "0002_telemetry",
        "0003_ux",
        "0004_errors",
    ]
    assert plan.max_parallel == 3


def test_the_intent_points_at_the_file_rather_than_inlining_it(specs: Path) -> None:
    """A spec pasted into a prompt goes stale the moment anyone edits it."""
    plan = _plan_from_specs(specs, [Path("docs/specs/0002_telemetry.md")])

    intent = plan.tasks[0].intent
    assert "docs/specs/0002_telemetry.md" in intent
    assert "out of scope" in intent, "siblings run concurrently; boundaries must bind"


def test_acceptance_includes_the_project_checks(specs: Path) -> None:
    plan = _plan_from_specs(specs, [Path("docs/specs/0003_ux.md")])

    acceptance = " ".join(plan.tasks[0].acceptance)
    assert "ruff" in acceptance
    assert "ty check" in acceptance
    assert "pytest" in acceptance


def test_a_missing_spec_fails_loudly(specs: Path) -> None:
    with pytest.raises(FileNotFoundError, match="no such spec"):
        _plan_from_specs(specs, [Path("docs/specs/9999_ghost.md")])


def test_absolute_paths_work_too(specs: Path) -> None:
    plan = _plan_from_specs(specs, [specs / "docs" / "specs" / "0004_errors.md"])

    assert plan.tasks[0].task_id == "0004_errors"
