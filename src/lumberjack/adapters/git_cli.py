"""``GitBackend`` implemented over the git CLI.

Two details carry most of the weight of the design:

* :meth:`GitCli.snapshot` writes a throwaway index and an unreferenced commit, so a
  worktree's *uncommitted* work becomes a real commit the oracle can merge, without
  the working directory being disturbed and without polluting any branch.
* :meth:`GitCli.merge` never checks anything out.  It merges with ``merge-tree``,
  writes the result with ``commit-tree`` and moves the ref with ``update-ref``, which
  means the integration branch needs no worktree of its own.

Requires git >= 2.38 for ``merge-tree --write-tree``.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from lumberjack.domain.vcs import (
    ChangeKind,
    FileChange,
    MergeOutcome,
    MergeTreeResult,
    RebaseOutcome,
    WorkingStatus,
)
from lumberjack.domain.workstream import Snapshot, Worktree
from lumberjack.ids import CommitSha, RepoPath, TreeSha, repo_path
from lumberjack.ports.git import GitError

__all__ = ["GitCli", "GitResult"]

_SAFE_CONFIG = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "advice.detachedHead=false",
    "-c",
    "gc.auto=0",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "tag.gpgsign=false",
)

_STATUS_MAP = {
    "A": ChangeKind.ADDED,
    "M": ChangeKind.MODIFIED,
    "D": ChangeKind.DELETED,
    "R": ChangeKind.RENAMED,
    "C": ChangeKind.ADDED,
    "T": ChangeKind.TYPE_CHANGED,
}


@dataclass(frozen=True, slots=True)
class GitResult:
    code: int
    out: str
    err: str
    raw: bytes = b""

    @property
    def ok(self) -> bool:
        return self.code == 0


@dataclass(slots=True)
class GitCli:
    """A git backend bound to one repository."""

    repo: Path
    git: str = field(default_factory=lambda: shutil.which("git") or "git")

    async def _run(
        self,
        *args: str,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        check: bool = True,
        stdin: bytes | None = None,
    ) -> GitResult:
        environ = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})}
        process = await asyncio.create_subprocess_exec(
            self.git,
            *_SAFE_CONFIG,
            *args,
            cwd=str(cwd or self.repo),
            env=environ,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        raw_out, raw_err = await process.communicate(stdin)
        result = GitResult(
            code=process.returncode or 0,
            out=raw_out.decode("utf-8", "replace"),
            err=raw_err.decode("utf-8", "replace"),
            raw=raw_out,
        )
        if check and not result.ok:
            raise GitError(args, result.code, result.err)
        return result

    # -- refs ------------------------------------------------------------------------

    async def resolve(self, ref: str) -> CommitSha:
        result = await self._run("rev-parse", "--verify", f"{ref}^{{commit}}")
        return CommitSha(result.out.strip())

    async def create_branch(self, name: str, at: CommitSha, *, force: bool = False) -> None:
        args = ["branch", name, at]
        if force:
            args.insert(1, "--force")
        await self._run(*args)

    async def delete_branch(self, name: str) -> None:
        await self._run("branch", "-D", name, check=False)

    async def head(self, worktree: Worktree) -> CommitSha:
        result = await self._run("rev-parse", "HEAD", cwd=worktree.path)
        return CommitSha(result.out.strip())

    # -- worktrees -------------------------------------------------------------------

    async def add_worktree(self, branch: str, base: CommitSha, at: Path) -> Worktree:
        at.parent.mkdir(parents=True, exist_ok=True)
        await self._run("worktree", "add", "--quiet", "-b", branch, str(at), base)
        return Worktree(path=at, branch=branch, base=base)

    async def remove_worktree(self, worktree: Worktree, *, force: bool = False) -> None:
        args = ["worktree", "remove", str(worktree.path)]
        if force:
            args.append("--force")
        await self._run(*args)

    async def list_worktrees(self) -> tuple[Worktree, ...]:
        result = await self._run("worktree", "list", "--porcelain")
        found: list[Worktree] = []
        path: Path | None = None
        commit: str | None = None
        for line in result.out.splitlines():
            if line.startswith("worktree "):
                path = Path(line.removeprefix("worktree ").strip())
            elif line.startswith("HEAD "):
                commit = line.removeprefix("HEAD ").strip()
            elif line.startswith("branch ") and path is not None and commit is not None:
                branch = line.removeprefix("branch ").strip().removeprefix("refs/heads/")
                found.append(Worktree(path=path, branch=branch, base=CommitSha(commit)))
                path, commit = None, None
        return tuple(found)

    # -- working state ---------------------------------------------------------------

    async def status(self, worktree: Worktree) -> WorkingStatus:
        result = await self._run("status", "--porcelain=v2", "-z", cwd=worktree.path)
        changes: list[FileChange] = []
        untracked: list[RepoPath] = []
        records = [item for item in result.raw.decode("utf-8", "replace").split("\0") if item]
        index = 0
        while index < len(records):
            record = records[index]
            index += 1
            if record.startswith("? "):
                untracked.append(repo_path(record[2:]))
            elif record.startswith(("1 ", "2 ")):
                fields = record.split(" ", 8)
                xy = fields[1]
                letter = next((ch for ch in xy if ch not in "."), "M")
                path_field = fields[-1]
                if record.startswith("2 "):
                    # rename/copy: the original path follows in the next NUL record
                    old = records[index] if index < len(records) else None
                    index += 1
                    changes.append(
                        FileChange(
                            path=repo_path(path_field),
                            kind=ChangeKind.RENAMED,
                            old_path=repo_path(old) if old else None,
                        )
                    )
                else:
                    changes.append(
                        FileChange(
                            path=repo_path(path_field),
                            kind=_STATUS_MAP.get(letter, ChangeKind.MODIFIED),
                        )
                    )
            elif record.startswith("u "):
                fields = record.split(" ", 10)
                changes.append(FileChange(path=repo_path(fields[-1]), kind=ChangeKind.MODIFIED))
        return WorkingStatus(changes=tuple(changes), untracked=tuple(untracked))

    async def snapshot(self, worktree: Worktree, *, message: str = "lj-snapshot") -> Snapshot:
        """Capture the worktree, including uncommitted work, as an unreferenced commit."""
        status = await self.status(worktree)
        head = await self.head(worktree)
        if not status.dirty:
            return Snapshot(commit=head, tree=await self._tree_of(head), dirty=False, paths=())

        with tempfile.TemporaryDirectory(prefix="lj-index-") as tmp:
            index_file = str(Path(tmp) / "index")
            env = {"GIT_INDEX_FILE": index_file}
            await self._run("read-tree", head, cwd=worktree.path, env=env)
            await self._run("add", "-A", "--", ".", cwd=worktree.path, env=env)
            tree = (await self._run("write-tree", cwd=worktree.path, env=env)).out.strip()
            commit = (
                await self._run(
                    "commit-tree", tree, "-p", head, "-m", message, cwd=worktree.path, env=env
                )
            ).out.strip()
        return Snapshot(
            commit=CommitSha(commit),
            tree=tree,
            dirty=True,
            paths=tuple(status.paths),
        )

    async def _tree_of(self, commit: CommitSha) -> str:
        result = await self._run("rev-parse", f"{commit}^{{tree}}")
        return result.out.strip()

    # -- merging ---------------------------------------------------------------------

    async def merge_base(self, left: CommitSha, right: CommitSha) -> CommitSha | None:
        result = await self._run("merge-base", left, right, check=False)
        return CommitSha(result.out.strip()) if result.ok and result.out.strip() else None

    async def merge_tree(
        self, ours: CommitSha, theirs: CommitSha, *, base: CommitSha | None = None
    ) -> MergeTreeResult:
        """A real merge with no worktree and no checkout.  Ground truth for conflict."""
        args = ["merge-tree", "--write-tree", "--messages", "--name-only"]
        if base is not None:
            args.append(f"--merge-base={base}")
        args.extend([ours, theirs])
        result = await self._run(*args, check=False)
        if result.code not in (0, 1):
            raise GitError(tuple(args), result.code, result.err)

        lines = result.out.split("\n")
        tree = lines[0].strip() if lines and lines[0].strip() else None
        clean = result.code == 0
        conflicted: list[RepoPath] = []
        messages = ""
        if not clean:
            body = lines[1:]
            blank = next((i for i, line in enumerate(body) if not line.strip()), len(body))
            conflicted = [repo_path(line.strip()) for line in body[:blank] if line.strip()]
            messages = "\n".join(body[blank:]).strip()
        return MergeTreeResult(
            clean=clean,
            tree=TreeSha(tree) if tree else None,
            conflicted=tuple(dict.fromkeys(conflicted)),
            messages=messages[:4000],
            merge_base=base,
        )

    async def merge(self, branch: str, into: str, *, message: str) -> MergeOutcome:
        """Merge without checking anything out: merge-tree, commit-tree, update-ref."""
        ours = await self.resolve(into)
        theirs = await self.resolve(branch)
        if ours == theirs:
            return MergeOutcome(status="up_to_date", head=ours)

        base = await self.merge_base(ours, theirs)
        if base == theirs:
            return MergeOutcome(status="up_to_date", head=ours)
        if base == ours:
            await self._run("update-ref", f"refs/heads/{into}", theirs, ours)
            return MergeOutcome(status="fast_forwarded", head=theirs)

        merged = await self.merge_tree(ours, theirs, base=base)
        if not merged.clean or merged.tree is None:
            return MergeOutcome(
                status="conflicted",
                head=ours,
                conflicted=merged.conflicted,
                detail=merged.messages,
            )
        commit = (
            await self._run("commit-tree", merged.tree, "-p", ours, "-p", theirs, "-m", message)
        ).out.strip()
        await self._run("update-ref", f"refs/heads/{into}", commit, ours)
        return MergeOutcome(status="merged", head=CommitSha(commit))

    async def rebase(self, worktree: Worktree, onto: CommitSha) -> RebaseOutcome:
        current = await self.head(worktree)
        if (await self.status(worktree)).dirty:
            return RebaseOutcome(
                status="failed",
                head=current,
                detail="worktree has uncommitted changes; commit or stash before rebasing",
            )
        base = await self.merge_base(current, onto)
        if base == onto:
            return RebaseOutcome(status="already_current", head=current)
        result = await self._run("rebase", onto, cwd=worktree.path, check=False)
        if result.ok:
            return RebaseOutcome(status="rebased", head=await self.head(worktree))
        conflicted = await self._conflicted_paths(worktree)
        await self._run("rebase", "--abort", cwd=worktree.path, check=False)
        return RebaseOutcome(
            status="conflicted" if conflicted else "failed",
            head=current,
            conflicted=conflicted,
            detail=result.err[:2000],
        )

    async def _conflicted_paths(self, worktree: Worktree) -> tuple[RepoPath, ...]:
        result = await self._run(
            "diff", "--name-only", "--diff-filter=U", "-z", cwd=worktree.path, check=False
        )
        return tuple(
            repo_path(item) for item in result.raw.decode("utf-8", "replace").split("\0") if item
        )

    # -- history ---------------------------------------------------------------------

    async def changes(self, base: CommitSha, tip: CommitSha) -> tuple[FileChange, ...]:
        if base == tip:
            return ()
        names = await self._run("diff", "-M", "--name-status", "-z", base, tip)
        numbers = await self._run("diff", "-M", "--numstat", "-z", base, tip)
        stats = _parse_numstat(numbers.raw.decode("utf-8", "replace"))
        changes: list[FileChange] = []
        for status, old, new in _parse_name_status(names.raw.decode("utf-8", "replace")):
            insertions, deletions = stats.get(new, (0, 0))
            changes.append(
                FileChange(
                    path=repo_path(new),
                    kind=_STATUS_MAP.get(status[0], ChangeKind.MODIFIED),
                    old_path=repo_path(old) if old else None,
                    insertions=insertions,
                    deletions=deletions,
                )
            )
        return tuple(changes)

    async def commits_between(self, base: CommitSha, tip: CommitSha) -> int:
        result = await self._run("rev-list", "--count", f"{base}..{tip}", check=False)
        return int(result.out.strip() or 0) if result.ok else 0

    async def read_blob(self, commit: CommitSha, path: RepoPath) -> bytes | None:
        result = await self._run("cat-file", "blob", f"{commit}:{path}", check=False)
        return result.raw if result.ok else None

    async def list_files(self, commit: CommitSha) -> tuple[RepoPath, ...]:
        result = await self._run("ls-tree", "-r", "--name-only", "-z", commit)
        return tuple(
            repo_path(item) for item in result.raw.decode("utf-8", "replace").split("\0") if item
        )

    async def churn(self, since: str = "200") -> dict[RepoPath, int]:
        result = await self._run(
            "log", f"-n{since}", "--pretty=format:", "--name-only", check=False
        )
        counts: dict[RepoPath, int] = {}
        for line in result.out.splitlines():
            name = line.strip()
            if name:
                key = repo_path(name)
                counts[key] = counts.get(key, 0) + 1
        return counts

    async def commit_all(self, worktree: Worktree, message: str) -> CommitSha | None:
        status = await self.status(worktree)
        if not status.dirty:
            return None
        await self._run("add", "-A", cwd=worktree.path)
        await self._run(
            "commit",
            "--no-verify",
            "--no-gpg-sign",
            "-m",
            message,
            cwd=worktree.path,
            env={
                "GIT_AUTHOR_NAME": "lumberjack",
                "GIT_AUTHOR_EMAIL": "lumberjack@localhost",
                "GIT_COMMITTER_NAME": "lumberjack",
                "GIT_COMMITTER_EMAIL": "lumberjack@localhost",
            },
        )
        return await self.head(worktree)


def _parse_name_status(payload: str) -> list[tuple[str, str | None, str]]:
    records = [item for item in payload.split("\0") if item]
    parsed: list[tuple[str, str | None, str]] = []
    index = 0
    while index < len(records):
        status = records[index]
        index += 1
        if status.startswith(("R", "C")) and index + 1 < len(records):
            old, new = records[index], records[index + 1]
            index += 2
            parsed.append((status, old, new))
        elif index < len(records):
            parsed.append((status, None, records[index]))
            index += 1
    return parsed


def _parse_numstat(payload: str) -> dict[str, tuple[int, int]]:
    records = [item for item in payload.split("\0") if item]
    stats: dict[str, tuple[int, int]] = {}
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        parts = record.split("\t")
        if len(parts) < 2:
            continue
        added = 0 if parts[0] == "-" else int(parts[0] or 0)
        removed = 0 if parts[1] == "-" else int(parts[1] or 0)
        if len(parts) >= 3 and parts[2]:
            stats[parts[2]] = (added, removed)
        elif index + 1 < len(records):
            stats[records[index + 1]] = (added, removed)
            index += 2
    return stats
