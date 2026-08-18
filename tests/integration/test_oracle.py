"""Golden conflict repositories.

These are the tests that decide whether the whole design holds up.  The claim in the
spec is that file-level exclusion is too coarse -- that two agents editing different
functions in one file merge cleanly -- and that ``git merge-tree`` can tell us which
case we are in before anyone commits.  If either half of that is wrong, the awareness
model is wrong.
"""

from __future__ import annotations

from pathlib import Path

from lumberjack.core.services import Services
from lumberjack.domain.conflict import ConflictSource, Severity

REFORMATTED = """\
def alpha() -> int:

    return 1


def beta() -> int:

    return 2


def gamma() -> int:

    return 3
"""


def edit(worktree: Path, old: str, new: str, path: str = "pkg/core.py") -> None:
    target = worktree / path
    text = target.read_text()
    assert old in text, f"fixture drift: {old!r} not in {path}"
    target.write_text(text.replace(old, new))


async def test_different_functions_in_one_file_merge_cleanly(
    services: Services, make_workstream
) -> None:
    """The premise of ``EDIT`` coexisting with ``EDIT``."""
    left = await make_workstream("a")
    right = await make_workstream("b")
    edit(left.worktree.path, "return 1", "return 111")
    edit(right.worktree.path, "return 3", "return 333")

    report = await services.oracle.probe_pair(left.workstream_id, right.workstream_id)

    assert report is None


async def test_same_function_conflicts(services: Services, make_workstream) -> None:
    left = await make_workstream("a")
    right = await make_workstream("b")
    edit(left.worktree.path, "return 1", "return 111")
    edit(right.worktree.path, "return 1", "return 999")

    report = await services.oracle.probe_pair(left.workstream_id, right.workstream_id)

    assert report is not None
    assert report.source is ConflictSource.MERGE_TREE
    assert report.severity is Severity.BLOCK
    assert "pkg/core.py" in report.paths


async def test_uncommitted_work_is_visible_to_the_oracle(
    services: Services, make_workstream
) -> None:
    """Agents never have to commit to be seen: snapshots capture the working tree."""
    left = await make_workstream("a")
    right = await make_workstream("b")
    edit(left.worktree.path, "return 2", "return 222")
    edit(right.worktree.path, "return 2", "return 888")

    # Nothing has been committed in either worktree.
    assert (await services.git.status(left.worktree)).dirty
    report = await services.oracle.probe_pair(left.workstream_id, right.workstream_id)

    assert report is not None
    assert "pkg/core.py" in report.paths


async def test_rename_versus_edit_merges_but_is_still_dangerous(
    services: Services, make_workstream
) -> None:
    """The case that justifies EXCLUSIVE existing at all.

    Git's rename detection merges this happily: the edit follows the file to its new
    name and the textual merge is clean.  What it cannot know is that every importer of
    ``pkg.core`` is now broken.  A textual oracle alone would wave this through, which
    is precisely why structural change needs an exclusive lease and the sensor raises a
    violation when it happens without one.
    """
    left = await make_workstream("a")
    right = await make_workstream("b")
    (left.worktree.path / "pkg" / "core.py").rename(left.worktree.path / "pkg" / "kernel.py")
    edit(right.worktree.path, "return 1", "return 999")

    report = await services.oracle.probe_pair(left.workstream_id, right.workstream_id)

    assert report is None, "git merges rename-vs-edit; the danger is semantic, not textual"


async def test_reformat_versus_edit_conflicts(services: Services, make_workstream) -> None:
    """Why mass reformatting has to be EXCLUSIVE: it collides with everything."""
    left = await make_workstream("a")
    right = await make_workstream("b")
    (left.worktree.path / "pkg" / "core.py").write_text(REFORMATTED)
    edit(right.worktree.path, "return 2", "return 222")

    report = await services.oracle.probe_pair(left.workstream_id, right.workstream_id)

    assert report is not None
    assert "pkg/core.py" in report.paths


async def test_disjoint_files_are_prefiltered(services: Services, make_workstream) -> None:
    left = await make_workstream("a")
    right = await make_workstream("b")
    edit(left.worktree.path, "return 1", "return 111")
    (right.worktree.path / "README.md").write_text("# changed\n")

    assert await services.oracle.probe_pair(left.workstream_id, right.workstream_id) is None


async def test_probe_all_reports_every_conflicting_pair(
    services: Services, make_workstream
) -> None:
    left = await make_workstream("a")
    middle = await make_workstream("b")
    right = await make_workstream("c")
    edit(left.worktree.path, "return 1", "return 111")
    edit(middle.worktree.path, "return 1", "return 222")
    edit(right.worktree.path, "return 3", "return 333")

    reports = await services.oracle.probe_all()

    pairs = {report.pair_key() for report in reports}
    assert (left.workstream_id, middle.workstream_id) in pairs
    assert not any(right.workstream_id in pair for pair in pairs)


async def test_clean_probe_clears_a_stale_conflict(services: Services, make_workstream) -> None:
    """P1: a warning the oracle disproves is cleared, not escalated."""
    left = await make_workstream("a")
    right = await make_workstream("b")
    edit(left.worktree.path, "return 1", "return 111")
    edit(right.worktree.path, "return 1", "return 999")
    first = await services.oracle.probe_pair(left.workstream_id, right.workstream_id)
    assert first is not None
    assert first.conflict_id in services.projections.conflicts

    edit(right.worktree.path, "return 999", "return 1")
    edit(right.worktree.path, "return 3", "return 333")
    assert await services.oracle.probe_pair(left.workstream_id, right.workstream_id) is None
    assert first.conflict_id not in services.projections.conflicts


async def test_would_land_cleanly_gates_the_train(services: Services, make_workstream) -> None:
    workstream = await make_workstream("a")
    edit(workstream.worktree.path, "return 1", "return 111")
    tip = await services.git.commit_all(workstream.worktree, "work")
    assert tip is not None
    services.projections.workstreams[workstream.workstream_id] = services.projections.workstreams[
        workstream.workstream_id
    ].model_copy(update={"tip": tip})

    clean, conflicted = await services.oracle.would_land_cleanly(workstream.workstream_id)

    assert clean
    assert conflicted == ()
