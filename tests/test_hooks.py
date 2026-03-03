from __future__ import annotations

import json
from pathlib import Path

from loops.core.hooks import (
    HookExecutor,
    StateHookRegistry,
    TaskStatusHook,
    TransitionContext,
)
from loops.task_providers.base import TaskStatus


class _RecordingProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, TaskStatus]] = []

    def update_status(self, task_id: str, status: TaskStatus) -> None:
        self.calls.append((task_id, status))


def _context(
    *,
    provider: _RecordingProvider | None = None,
    logger: list[str] | None = None,
) -> TransitionContext:
    sink = [] if logger is None else logger
    return TransitionContext(
        run_id="run-1",
        task_id="task-123",
        task_provider=provider,
        from_state=None,
        to_state="RUNNING",
        logger=lambda message: sink.append(message),
    )


def test_hook_registry_executes_in_registration_order(tmp_path: Path) -> None:
    registry = StateHookRegistry()
    observed: list[str] = []
    logs: list[str] = []
    context = _context(logger=logs)
    registry.register_on_enter(
        "RUNNING",
        hook_id="first",
        callback=lambda _ctx: observed.append("first"),
    )
    registry.register_on_enter(
        "RUNNING",
        hook_id="second",
        callback=lambda _ctx: observed.append("second"),
    )
    executor = HookExecutor(
        run_dir=tmp_path,
        registry=registry,
        logger=lambda message: logs.append(message),
    )

    executor.execute_on_enter(state="RUNNING", context=context)

    assert observed == ["first", "second"]


def test_hook_executor_dedupes_by_run_phase_state_and_hook_id(tmp_path: Path) -> None:
    registry = StateHookRegistry()
    counter = {"value": 0}
    logs: list[str] = []
    context = _context(logger=logs)
    registry.register_on_enter(
        "RUNNING",
        hook_id="OnceOnlyHook",
        callback=lambda _ctx: counter.__setitem__("value", counter["value"] + 1),
    )
    executor = HookExecutor(
        run_dir=tmp_path,
        registry=registry,
        logger=lambda message: logs.append(message),
    )

    executor.execute_on_enter(state="RUNNING", context=context)
    executor.execute_on_enter(state="RUNNING", context=context)

    assert counter["value"] == 1
    payload = json.loads((tmp_path / "state_hooks.json").read_text())
    assert payload == {"executed": ["run-1:enter:RUNNING:OnceOnlyHook"]}


def test_hook_executor_logs_and_continues_when_hook_fails(tmp_path: Path) -> None:
    registry = StateHookRegistry()
    observed: list[str] = []
    logs: list[str] = []
    context = _context(logger=logs)
    registry.register_on_enter(
        "RUNNING",
        hook_id="FailingHook",
        callback=lambda _ctx: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    registry.register_on_enter(
        "RUNNING",
        hook_id="HealthyHook",
        callback=lambda _ctx: observed.append("healthy"),
    )
    executor = HookExecutor(
        run_dir=tmp_path,
        registry=registry,
        logger=lambda message: logs.append(message),
    )

    executor.execute_on_enter(state="RUNNING", context=context)

    assert observed == ["healthy"]
    assert any("hook failed" in entry for entry in logs)


def test_task_status_hook_updates_running_and_done_once(tmp_path: Path) -> None:
    registry = StateHookRegistry()
    TaskStatusHook(registry)
    logs: list[str] = []
    provider = _RecordingProvider()
    executor = HookExecutor(
        run_dir=tmp_path,
        registry=registry,
        logger=lambda message: logs.append(message),
    )
    context = _context(provider=provider, logger=logs)

    executor.execute_on_enter(state="RUNNING", context=context)
    executor.execute_on_enter(state="RUNNING", context=context)
    executor.execute_on_enter(
        state="DONE",
        context=TransitionContext(
            run_id="run-1",
            task_id="task-123",
            task_provider=provider,
            from_state="PR_APPROVED",
            to_state="DONE",
            logger=lambda message: logs.append(message),
        ),
    )

    assert provider.calls == [
        ("task-123", "IN_PROGRESS"),
        ("task-123", "DONE"),
    ]


def test_task_status_hook_skips_when_provider_missing(tmp_path: Path) -> None:
    registry = StateHookRegistry()
    TaskStatusHook(registry)
    logs: list[str] = []
    executor = HookExecutor(
        run_dir=tmp_path,
        registry=registry,
        logger=lambda message: logs.append(message),
    )

    executor.execute_on_enter(state="RUNNING", context=_context(provider=None, logger=logs))

    assert any("missing_provider_or_task_id" in entry for entry in logs)
