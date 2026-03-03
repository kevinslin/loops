from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Mapping

from loops.state.constants import STATE_HOOKS_LEDGER_FILE
from loops.state.run_record import RunState, VALID_RUN_STATES
from loops.task_providers.base import TaskProvider, TaskStatus

HookPhase = Literal["enter", "exit"]
HookFn = Callable[["TransitionContext"], None]
LoggerFn = Callable[[str], None]


@dataclass(frozen=True)
class TransitionContext:
    run_id: str
    task_id: str | None
    task_provider: TaskProvider | None
    from_state: RunState | None
    to_state: RunState
    logger: LoggerFn

    def log(self, message: str) -> None:
        self.logger(f"[loops][hooks] {message}")


@dataclass(frozen=True)
class _RegisteredHook:
    hook_id: str
    callback: HookFn


class StateHookRegistry:
    def __init__(self) -> None:
        self._registry: dict[HookPhase, dict[RunState, list[_RegisteredHook]]] = {
            "enter": {},
            "exit": {},
        }

    def register_on_enter(
        self,
        state: RunState,
        *,
        hook_id: str,
        callback: HookFn,
    ) -> None:
        self._register(phase="enter", state=state, hook_id=hook_id, callback=callback)

    def register_on_exit(
        self,
        state: RunState,
        *,
        hook_id: str,
        callback: HookFn,
    ) -> None:
        self._register(phase="exit", state=state, hook_id=hook_id, callback=callback)

    def hooks_for(self, *, phase: HookPhase, state: RunState) -> tuple[_RegisteredHook, ...]:
        return tuple(self._registry.get(phase, {}).get(state, ()))

    def _register(
        self,
        *,
        phase: HookPhase,
        state: RunState,
        hook_id: str,
        callback: HookFn,
    ) -> None:
        _validate_state(state)
        if not hook_id.strip():
            raise ValueError("hook_id must be non-empty")
        self._registry.setdefault(phase, {}).setdefault(state, []).append(
            _RegisteredHook(hook_id=hook_id, callback=callback)
        )


class HookExecutor:
    def __init__(
        self,
        *,
        run_dir: Path,
        registry: StateHookRegistry,
        logger: LoggerFn,
    ) -> None:
        self._registry = registry
        self._logger = logger
        self._ledger_path = run_dir / STATE_HOOKS_LEDGER_FILE
        self._executed = _load_hook_ledger(self._ledger_path, logger=logger)

    def execute_on_enter(self, *, state: RunState, context: TransitionContext) -> None:
        self._execute(phase="enter", state=state, context=context)

    def execute_on_exit(self, *, state: RunState, context: TransitionContext) -> None:
        self._execute(phase="exit", state=state, context=context)

    def _execute(
        self,
        *,
        phase: HookPhase,
        state: RunState,
        context: TransitionContext,
    ) -> None:
        for hook in self._registry.hooks_for(phase=phase, state=state):
            key = f"{context.run_id}:{phase}:{state}:{hook.hook_id}"
            if key in self._executed:
                context.log(
                    f"skip duplicate hook phase={phase} state={state} hook={hook.hook_id}"
                )
                continue
            try:
                hook.callback(context)
            except Exception as exc:  # pragma: no cover - defensive hook isolation
                context.log(
                    f"hook failed phase={phase} state={state} hook={hook.hook_id}: {exc}"
                )
                continue

            self._executed.add(key)
            _write_hook_ledger(self._ledger_path, executed=self._executed, logger=self._logger)
            context.log(f"hook executed phase={phase} state={state} hook={hook.hook_id}")


class TaskStatusHook:
    def __init__(self, registry: StateHookRegistry) -> None:
        hook_id = self.__class__.__name__
        registry.register_on_enter(
            "RUNNING",
            hook_id=hook_id,
            callback=self._on_running_enter,
        )
        registry.register_on_enter(
            "DONE",
            hook_id=hook_id,
            callback=self._on_done_enter,
        )

    def _on_running_enter(self, context: TransitionContext) -> None:
        _update_task_status(context=context, status="IN_PROGRESS")

    def _on_done_enter(self, context: TransitionContext) -> None:
        _update_task_status(context=context, status="DONE")


def build_default_hook_executor(
    *,
    run_dir: Path,
    logger: LoggerFn,
) -> HookExecutor:
    registry = StateHookRegistry()
    # Registration order is execution order.
    TaskStatusHook(registry)
    return HookExecutor(
        run_dir=run_dir,
        registry=registry,
        logger=logger,
    )


def _update_task_status(*, context: TransitionContext, status: TaskStatus) -> None:
    task_provider = context.task_provider
    task_id = context.task_id
    if task_provider is None or task_id is None:
        context.log(
            f"task status update skipped status={status} reason=missing_provider_or_task_id"
        )
        return
    task_provider.update_status(task_id, status)
    context.log(f"task status updated status={status} task_id={task_id}")


def _validate_state(state: RunState) -> None:
    if state not in VALID_RUN_STATES:
        raise ValueError(f"unsupported run state for hooks: {state}")


def _load_hook_ledger(path: Path, *, logger: LoggerFn) -> set[str]:
    if not path.exists():
        return set()
    try:
        payload = json.loads(path.read_text())
    except Exception as exc:
        logger(f"[loops][hooks] invalid hook ledger; resetting path={path}: {exc}")
        return set()
    if not isinstance(payload, Mapping):
        logger(f"[loops][hooks] invalid hook ledger payload; resetting path={path}")
        return set()
    entries = payload.get("executed")
    if not isinstance(entries, list):
        logger(f"[loops][hooks] invalid hook ledger entries; resetting path={path}")
        return set()
    return {entry for entry in entries if isinstance(entry, str) and entry}


def _write_hook_ledger(path: Path, *, executed: set[str], logger: LoggerFn) -> None:
    try:
        payload = {"executed": sorted(executed)}
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    except Exception as exc:  # pragma: no cover - defensive filesystem handling
        logger(f"[loops][hooks] failed to persist hook ledger path={path}: {exc}")
