"""How a stand is observed.

Off by default, and *quiet* by default: a harness that ships repository content to a
collector the first time someone turns tracing on is a data leak with a progress bar.
``capture_content`` is the single switch that allows prompts and responses off the
machine, and 0001_SPEC.md §14 is why it defaults to ``False``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = ["Exporter", "TelemetryConfig"]


class Exporter(StrEnum):
    """Where spans and metrics go."""

    NONE = "none"
    OTLP = "otlp"
    CONSOLE = "console"


class TelemetryConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    exporter: Exporter = Exporter.NONE
    endpoint: str | None = Field(
        default=None,
        description="OTLP/HTTP collector base URL; the SDK's own env vars apply when unset.",
    )
    service_name: str = "lumberjack"
    capture_content: bool = Field(
        default=False,
        description=(
            "Whether prompts and model responses may be exported. Repository content is "
            "sensitive; leaving this off keeps token counts flowing and text at home."
        ),
    )

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.enabled and self.exporter is Exporter.NONE:
            msg = "telemetry is enabled but the exporter is 'none'; choose otlp or console"
            raise ValueError(msg)
        return self
