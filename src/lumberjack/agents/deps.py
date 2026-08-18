"""Typed dependencies for the PydanticAI agents."""

from __future__ import annotations

from dataclasses import dataclass

from lumberjack.core.services import Services
from lumberjack.domain.symbols import RepoMap
from lumberjack.domain.task import TaskSpec
from lumberjack.domain.workstream import Worktree
from lumberjack.ids import AgentId, TaskId, WorkstreamId

__all__ = ["ForemanDeps", "NegotiatorDeps", "ScoutDeps", "WorkerDeps"]


@dataclass(frozen=True, slots=True)
class WorkerDeps:
    services: Services
    identity: AgentId
    workstream: WorkstreamId
    task: TaskSpec
    worktree: Worktree

    @property
    def task_id(self) -> TaskId:
        return self.task.task_id


@dataclass(frozen=True, slots=True)
class ForemanDeps:
    services: Services
    identity: AgentId
    goal: str
    repo_map: RepoMap


@dataclass(frozen=True, slots=True)
class ScoutDeps:
    services: Services
    identity: AgentId


@dataclass(frozen=True, slots=True)
class NegotiatorDeps:
    services: Services
    identity: AgentId
    workstream: WorkstreamId
    peer: AgentId
