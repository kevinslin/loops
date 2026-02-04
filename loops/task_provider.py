from __future__ import annotations

from typing import Protocol

from loops.run_record import Task


class TaskProvider(Protocol):
    def poll(self, limit: int | None = None) -> list[Task]:
        """Return a list of tasks from the provider."""
        ...
