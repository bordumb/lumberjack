"""Task-state bookkeeping shared by the supervisor and the merge train."""

from __future__ import annotations

from lumberjack.core.projections import Projections
from lumberjack.domain.events import TaskStateChanged
from lumberjack.domain.task import Task
from lumberjack.ids import AgentId
from lumberjack.ports.ledger import Ledger

__all__ = ["record_transition"]


async def record_transition(
    ledger: Ledger,
    projections: Projections,
    new_state: Task,
    *,
    actor: AgentId | None = None,
    detail: str = "",
) -> Task:
    task_id = new_state.spec.task_id
    previous = projections.tasks.get(task_id)
    await ledger.append(
        TaskStateChanged(
            task_id=task_id,
            frm=previous.kind if previous is not None else "unknown",
            to=new_state.kind,
            state=new_state,
            detail=detail,
        ),
        actor=actor or "system",
    )
    projections.tasks[task_id] = new_state
    return new_state
