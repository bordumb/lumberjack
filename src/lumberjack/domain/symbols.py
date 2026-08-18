"""Symbol references and repository maps."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from lumberjack.ids import RepoPath

__all__ = ["ModuleNode", "RepoMap", "SymbolRef", "signature_digest"]


class SymbolRef(BaseModel):
    """A named, addressable definition in the repository."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    module: str = Field(description="Dotted, repo-relative module path.")
    qualname: str = Field(description="Qualified name within the module.")
    path: RepoPath
    signature: str | None = Field(
        default=None,
        description="Normalized signature text, when the indexer can render one.",
    )

    def __str__(self) -> str:
        return f"{self.module}:{self.qualname}"

    @property
    def key(self) -> tuple[str, str]:
        """Identity ignoring the signature, so a changed signature is still the same symbol."""
        return (self.module, self.qualname)


class ModuleNode(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    module: str
    path: RepoPath
    imports: frozenset[str] = frozenset()
    symbols: tuple[SymbolRef, ...] = ()
    churn: int = Field(default=0, ge=0, description="Commits touching this file in the window.")


class RepoMap(BaseModel):
    """Static picture of the repository, produced once per stand by the Scout."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    modules: tuple[ModuleNode, ...] = ()
    test_paths: tuple[RepoPath, ...] = ()

    def by_module(self) -> dict[str, ModuleNode]:
        return {node.module: node for node in self.modules}

    def by_path(self) -> dict[RepoPath, ModuleNode]:
        return {node.path: node for node in self.modules}

    def importers_of(self, module: str) -> frozenset[str]:
        return frozenset(node.module for node in self.modules if module in node.imports)

    def hot_paths(self, limit: int = 10) -> tuple[RepoPath, ...]:
        ranked = sorted(self.modules, key=lambda node: node.churn, reverse=True)
        return tuple(node.path for node in ranked[:limit])


def signature_digest(symbols: tuple[SymbolRef, ...]) -> str:
    """Stable digest over a set of symbol signatures.

    Used to detect that a frozen contract surface changed shape without having to
    diff source text.
    """
    from hashlib import blake2b

    digest = blake2b(digest_size=16)
    for symbol in sorted(symbols, key=lambda item: (item.module, item.qualname)):
        digest.update(symbol.module.encode())
        digest.update(b"\0")
        digest.update(symbol.qualname.encode())
        digest.update(b"\0")
        digest.update((symbol.signature or "").encode())
        digest.update(b"\n")
    return digest.hexdigest()
