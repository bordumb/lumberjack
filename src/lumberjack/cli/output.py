"""The one place ``lj`` writes to a stream.

Everything a command wants to say goes through an :class:`Output`.  There is no
``print`` anywhere in the package, which is what makes ``--json`` trustworthy: a stray
progress line in the middle of a document is a parse error for whoever is piping it.

Two streams, deliberately: renderables go to stdout, diagnostics go to stderr.  A
script running ``lj status --json | jq`` should still see the reason it failed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import IO

from rich.console import Console, RenderableType
from rich.text import Text

__all__ = ["Output", "default_output", "set_default_output"]


@dataclass(slots=True)
class Output:
    """A console pair, plus the one decision the whole rendering layer branches on.

    ``rich`` is true when stdout is a terminal.  When it is not -- ``lj run > log.txt``,
    or CI -- the live view degrades to one line per event rather than redrawing a frame
    a person will never see.
    """

    stdout: Console
    stderr: Console

    @classmethod
    def open(cls, *, force_terminal: bool | None = None, width: int | None = None) -> Output:
        return cls(
            stdout=Console(force_terminal=force_terminal, width=width, soft_wrap=False),
            stderr=Console(stderr=True, force_terminal=force_terminal, width=width),
        )

    @classmethod
    def to(cls, file: IO[str], *, width: int = 100) -> Output:
        """An output bound to a stream, for tests and for anything capturing ``lj``."""
        console = Console(file=file, width=width, force_terminal=False, highlight=False)
        return cls(stdout=console, stderr=console)

    @property
    def rich(self) -> bool:
        """Whether a live, redrawing view makes sense on this stdout."""
        return self.stdout.is_terminal

    def emit(self, renderable: RenderableType) -> None:
        self.stdout.print(renderable)

    def line(self, text: str = "", *, style: str = "") -> None:
        """A single line of plain text.  Never interprets markup: content is not markup."""
        self.stdout.print(Text(text, style=style))

    def json(self, payload: object) -> None:
        """A machine-readable document.

        Written unhighlighted and unwrapped: rich's word wrapping would insert newlines
        into long identifier lists, and ``jq`` does not forgive that.
        """
        document = json.dumps(payload, indent=2, default=str)
        self.stdout.print(document, markup=False, highlight=False, soft_wrap=True)

    def problem(self, renderable: RenderableType) -> None:
        """Something the operator has to act on.  Goes to stderr, so pipes stay clean."""
        self.stderr.print(renderable)


_DEFAULT: list[Output | None] = [None]


def default_output() -> Output:
    """The output every command writes to.

    Deliberately mutable: a test that wants to read what ``lj status`` printed swaps in
    an :meth:`Output.to` over a buffer, and the commands need no argument threading.
    """
    current = _DEFAULT[0]
    if current is None:
        current = Output.open()
        _DEFAULT[0] = current
    return current


def set_default_output(output: Output | None) -> None:
    """Point every command at another output.  ``None`` restores the real console."""
    _DEFAULT[0] = output
