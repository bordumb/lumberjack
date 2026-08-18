"""The scout: a repository map for scope prediction.

Deliberately *not* a language model.  The map is a question with a correct answer --
which module imports which, where the tests are, what churns -- and a wrong dependency
edge causes a missed conflict.  Read it from the repository instead of guessing at it.
"""

from __future__ import annotations

from dataclasses import dataclass

from lumberjack.domain.symbols import ModuleNode, RepoMap
from lumberjack.ids import CommitSha, RepoPath
from lumberjack.ports.git import GitBackend
from lumberjack.ports.indexer import SymbolIndexer

__all__ = ["Scout"]

_TEST_MARKERS = ("tests/", "test_", "_test.py", "/conftest.py")


@dataclass(frozen=True, slots=True)
class Scout:
    git: GitBackend
    indexer: SymbolIndexer
    max_files: int = 2000

    async def survey(self, commit: CommitSha) -> RepoMap:
        paths = await self.git.list_files(commit)
        churn = await self.git.churn("300")
        modules: list[ModuleNode] = []
        tests: list[RepoPath] = []

        for path in paths[: self.max_files]:
            if _is_test(path):
                tests.append(path)
            if not path.endswith(".py"):
                continue
            blob = await self.git.read_blob(commit, path)
            if blob is None:
                continue
            modules.append(
                ModuleNode(
                    module=self.indexer.module_name(path),
                    path=path,
                    imports=await self.indexer.imports_in(path, blob),
                    symbols=await self.indexer.symbols_in(path, blob),
                    churn=churn.get(path, 0),
                )
            )
        return RepoMap(modules=tuple(modules), test_paths=tuple(tests))


def _is_test(path: RepoPath) -> bool:
    return any(marker in path for marker in _TEST_MARKERS)
