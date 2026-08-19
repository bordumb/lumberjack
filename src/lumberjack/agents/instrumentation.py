"""How much of an agent run is allowed to leave the machine.

PydanticAI emits its own spans for model requests and tool calls, which is most of the
tracing this project would otherwise have to write.  Its default, though, is to attach
prompts and completions to those spans -- and a prompt here contains repository source.
0001_SPEC.md §14 says secrets never leave the machine, so the default is inverted at the
one place the agents are built, and turning it back on is a deliberate act.

Only the OpenTelemetry API is imported, never the SDK, so this costs nothing to a
checkout without the ``telemetry`` extra.
"""

from __future__ import annotations

from pydantic_ai.models.instrumented import InstrumentationSettings

__all__ = ["QUIET", "instrumentation"]


def instrumentation(*, capture_content: bool = False) -> InstrumentationSettings:
    """Spans always; prompt, response and binary content only when explicitly allowed."""
    return InstrumentationSettings(
        include_content=capture_content,
        include_binary_content=capture_content,
    )


QUIET = instrumentation()
"""The default every builder falls back to: instrumented, and silent about content."""
