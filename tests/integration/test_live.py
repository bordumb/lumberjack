"""The one test that talks to a real model.

Opt in with ``uv run pytest -m live``.  Everything else in the suite runs against
``FunctionModel``, because a harness whose tests need a model provider is a harness
nobody runs.
"""

from __future__ import annotations

import os

import pytest

from lumberjack.agents.outputs import Plan
from lumberjack.core.services import Services
from lumberjack.core.supervisor import Supervisor
from lumberjack.domain.task import TaskSpec
from lumberjack.ids import TaskId

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY is not set"
    ),
]


async def test_three_real_agents_land_real_work(services: Services) -> None:
    supervisor = Supervisor(services=services)
    plan = Plan(
        tasks=(
            TaskSpec(
                task_id=TaskId("task-alpha"),
                title="alpha returns 111",
                intent=(
                    "In pkg/core.py, change the function alpha so it returns 111 instead "
                    "of 1. Leave beta and gamma exactly as they are."
                ),
                acceptance=("alpha() returns 111", "beta and gamma are unchanged"),
            ),
            TaskSpec(
                task_id=TaskId("task-gamma"),
                title="gamma returns 333",
                intent=(
                    "In pkg/core.py, change the function gamma so it returns 333 instead "
                    "of 3. Leave alpha and beta exactly as they are."
                ),
                acceptance=("gamma() returns 333", "alpha and beta are unchanged"),
            ),
        ),
        max_parallel=2,
    )

    outcome = await supervisor.run("raise the constants", plan=plan)

    assert outcome.landed, outcome.summary()
    head = services.projections.integration_head
    assert head is not None
    merged = (await services.git.read_blob(head, "pkg/core.py") or b"").decode()
    assert "111" in merged or "333" in merged
