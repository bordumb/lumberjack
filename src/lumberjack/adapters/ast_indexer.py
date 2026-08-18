"""Symbol extraction for Python via the standard-library ``ast`` module.

Good enough for the milestones that matter, and deliberately a port so a tree-sitter
adapter can replace it for polyglot repositories without touching the core.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from lumberjack.domain.symbols import RepoMap, SymbolRef
from lumberjack.ids import RepoPath, repo_path

__all__ = ["AstIndexer", "render_signature"]


def render_signature(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> str:
    """A normalized signature string: stable under reformatting, unstable under real change."""
    if isinstance(node, ast.ClassDef):
        bases = ", ".join(ast.unparse(base) for base in node.bases)
        return f"class {node.name}({bases})"
    args = node.args
    rendered: list[str] = []
    for group, prefix in ((args.posonlyargs, ""), (args.args, "")):
        rendered.extend(f"{prefix}{arg.arg}: {_annotation(arg)}" for arg in group)
    if args.posonlyargs:
        rendered.insert(len(args.posonlyargs), "/")
    if args.vararg is not None:
        rendered.append(f"*{args.vararg.arg}: {_annotation(args.vararg)}")
    elif args.kwonlyargs:
        rendered.append("*")
    rendered.extend(f"{arg.arg}: {_annotation(arg)}" for arg in args.kwonlyargs)
    if args.kwarg is not None:
        rendered.append(f"**{args.kwarg.arg}: {_annotation(args.kwarg)}")
    returns = ast.unparse(node.returns) if node.returns is not None else "Any"
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    return f"{prefix} {node.name}({', '.join(rendered)}) -> {returns}"


def _annotation(arg: ast.arg) -> str:
    return ast.unparse(arg.annotation) if arg.annotation is not None else "Any"


@dataclass(frozen=True, slots=True)
class AstIndexer:
    package_roots: tuple[str, ...] = ("src", "")

    def module_name(self, path: RepoPath) -> str:
        trimmed = path
        for root in self.package_roots:
            prefix = f"{root}/"
            if root and trimmed.startswith(prefix):
                trimmed = repo_path(trimmed.removeprefix(prefix))
                break
        stem = trimmed.removesuffix(".py")
        stem = stem.removesuffix("/__init__")
        return stem.replace("/", ".")

    async def symbols_in(self, path: RepoPath, blob: bytes) -> tuple[SymbolRef, ...]:
        if not path.endswith(".py"):
            return ()
        try:
            tree = ast.parse(blob.decode("utf-8", "replace"))
        except SyntaxError:
            return ()
        module = self.module_name(path)
        found: list[SymbolRef] = []

        def walk(body: list[ast.stmt], prefix: str) -> None:
            for node in body:
                if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                    qualname = f"{prefix}{node.name}"
                    found.append(
                        SymbolRef(
                            module=module,
                            qualname=qualname,
                            path=path,
                            signature=render_signature(node),
                        )
                    )
                    if isinstance(node, ast.ClassDef):
                        walk(node.body, f"{qualname}.")

        walk(tree.body, "")
        return tuple(found)

    async def imports_in(self, path: RepoPath, blob: bytes) -> frozenset[str]:
        if not path.endswith(".py"):
            return frozenset()
        try:
            tree = ast.parse(blob.decode("utf-8", "replace"))
        except SyntaxError:
            return frozenset()
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                modules.add(node.module)
                modules.update(f"{node.module}.{alias.name}" for alias in node.names)
        return frozenset(modules)

    async def dependents_of(
        self, symbol: SymbolRef, repo_map: RepoMap, *, depth: int = 2
    ) -> frozenset[SymbolRef]:
        """Symbols that would break if ``symbol`` changed shape, up to ``depth`` hops."""
        by_module = repo_map.by_module()
        frontier = {symbol.module}
        seen = {symbol.module}
        dependents: set[SymbolRef] = set()
        for _ in range(max(0, depth)):
            wave: set[str] = set()
            for node in repo_map.modules:
                if node.module in seen:
                    continue
                if any(
                    imported == target or imported.startswith(f"{target}.")
                    for target in frontier
                    for imported in node.imports
                ):
                    wave.add(node.module)
            if not wave:
                break
            for module in wave:
                seen.add(module)
                found = by_module.get(module)
                if found is not None:
                    dependents.update(found.symbols)
            frontier = wave
        return frozenset(dependents)
