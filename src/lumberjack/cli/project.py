"""What kind of repository is this, and therefore what does its gate run?

``lj init`` used to write ``ruff``, ``ty`` and ``pytest`` into every repository it
touched, which meant that the first thing a Node project's swarm learned was that the
gate is broken and can be ignored.  A gate nobody believes is worse than no gate.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

__all__ = ["GateDetection", "detect_gate"]

log = logging.getLogger(__name__)

PYTHON_GATE: tuple[tuple[str, ...], ...] = (
    ("uv", "run", "ruff", "check", "."),
    ("uv", "run", "ty", "check"),
    ("uv", "run", "pytest", "-q"),
)

NODE_SCRIPTS: tuple[str, ...] = ("lint", "typecheck", "test")
"""The conventional script names, in the order a gate should run them."""

UNKNOWN_PROJECT = "no pyproject.toml and no package.json, so there is no gate to infer"


@dataclass(frozen=True, slots=True)
class GateDetection:
    """The commands to write, and the sentence explaining why those ones."""

    commands: tuple[tuple[str, ...], ...]
    project: str
    why: str

    @property
    def empty(self) -> bool:
        return not self.commands


def detect_gate(repo: Path) -> GateDetection:
    """Pick the gate for this repository from what is actually in it."""
    if (repo / "pyproject.toml").is_file():
        return GateDetection(
            commands=PYTHON_GATE,
            project="python",
            why="pyproject.toml: ruff, ty and pytest under uv",
        )
    package = repo / "package.json"
    if package.is_file():
        return _node_gate(package)
    return GateDetection(commands=(), project="unknown", why=UNKNOWN_PROJECT)


def _node_gate(package: Path) -> GateDetection:
    """The declared scripts, in gate order.  Only the ones the project actually has."""
    scripts = _scripts(package)
    found = tuple(("npm", "run", name) for name in NODE_SCRIPTS if name in scripts)
    if found:
        names = ", ".join(name for name in NODE_SCRIPTS if name in scripts)
        return GateDetection(commands=found, project="node", why=f"package.json scripts: {names}")
    # A package.json with no recognizable script still says which ecosystem this is,
    # and `npm test` is the one command every one of them answers to.
    return GateDetection(
        commands=(("npm", "test"),),
        project="node",
        why="package.json with no lint/typecheck/test script; falling back to `npm test`",
    )


def _scripts(package: Path) -> frozenset[str]:
    try:
        document = json.loads(package.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        # A package.json we cannot read still identifies the ecosystem, and refusing to
        # initialize the repository over it would be a worse trade than a coarser gate.
        log.warning("could not read %s (%s); assuming no scripts", package, error)
        return frozenset()
    scripts = document.get("scripts") if isinstance(document, dict) else None
    return frozenset(scripts) if isinstance(scripts, dict) else frozenset()
