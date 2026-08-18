"""Symbol extraction and the reverse-dependency graph."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from lumberjack.domain.symbols import RepoMap, SymbolRef
    from lumberjack.ids import RepoPath

__all__ = ["SymbolIndexer"]


class SymbolIndexer(Protocol):
    def module_name(self, path: RepoPath) -> str: ...

    async def symbols_in(self, path: RepoPath, blob: bytes) -> tuple[SymbolRef, ...]: ...

    async def imports_in(self, path: RepoPath, blob: bytes) -> frozenset[str]: ...

    async def dependents_of(
        self, symbol: SymbolRef, repo_map: RepoMap, *, depth: int = 2
    ) -> frozenset[SymbolRef]: ...

    """Symbols that would break if ``symbol`` changed shape, up to ``depth`` hops."""
