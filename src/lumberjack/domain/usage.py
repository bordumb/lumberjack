"""What a stand has spent.

Kept separate from the ledger of *events* on purpose: usage is a running total, not a
history.  Turning every model call into a database write would make the cheapest thing
the harness does the most expensive, so these totals live in memory and are additive,
which is all a caller polling them for a budget or a dashboard needs.
"""

from __future__ import annotations

from datetime import timedelta

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["UsageTotals"]


class UsageTotals(BaseModel):
    """An additive tally of what one agent, one workstream or a whole stand has spent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    requests: int = Field(default=0, ge=0)
    steps: int = Field(default=0, ge=0)
    """Agent graph steps -- the number ``Budget.max_steps_per_task`` bounds."""
    tool_calls: int = Field(default=0, ge=0)
    wall_clock: timedelta = timedelta(0)

    @property
    def total_tokens(self) -> int:
        """Every token the provider billed, cached reads and writes included."""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    def __add__(self, other: UsageTotals) -> UsageTotals:
        return UsageTotals(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            requests=self.requests + other.requests,
            steps=self.steps + other.steps,
            tool_calls=self.tool_calls + other.tool_calls,
            wall_clock=self.wall_clock + other.wall_clock,
        )

    def render(self) -> str:
        """One line, for a log or a status pane.  Formatting beyond this belongs to 0003."""
        return (
            f"{self.total_tokens} tokens "
            f"({self.input_tokens} in, {self.output_tokens} out) "
            f"over {self.requests} request(s)"
        )
