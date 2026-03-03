from __future__ import annotations

from typing import Literal, Protocol

from loops.state.run_record import Task

TaskStatus = Literal["TODO", "IN_PROGRESS", "DONE"]


class TaskProvider(Protocol):
    def poll(self, limit: int | None = None) -> list[Task]:
        """Return a list of tasks from the provider."""
        ...

    def update_status(self, task_id: str, status: TaskStatus) -> None:
        """Update provider task status idempotently."""
        ...
