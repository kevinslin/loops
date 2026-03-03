from __future__ import annotations

"""Outer loop runner utilities for the Loops harness."""

import json
import os
import re
import shlex
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit, urlunsplit

from pydantic import ValidationError

from loops.state.approval_config import (
    DEFAULT_APPROVAL_COMMENT_PATTERN,
    normalize_approval_usernames,
)
from loops.core.handoff_handlers import (
    DEFAULT_HANDOFF_HANDLER,
    validate_handoff_handler_name,
    validate_handoff_handler_provider_compatibility,
)
from loops.state.inner_loop_runtime_config import (
    InnerLoopRuntimeConfig,
    write_inner_loop_runtime_config,
)
from loops.state.constants import (
    AGENT_LOG_FILE_NAME,
    CHECKOUT_MODE_BRANCH,
    CHECKOUT_MODE_WORKTREE,
    INNER_LOOP_RUNS_DIR_NAME,
    LATEST_LOOPS_CONFIG_VERSION,
    OUTER_LOG_FILE_NAME,
    OUTER_STATE_FILE_NAME,
    RUN_LOG_FILE_NAME,
    RUN_RECORD_FILE_NAME,
    VALID_CHECKOUT_MODES,
)
from loops.utils.logging import format_log_timestamp
from loops.state.provider_types import LoopsProviderConfig, SecretRequirement
from loops.task_providers.github_projects_v2 import (
    GITHUB_PROJECTS_V2_PROVIDER_ID,
    build_default_provider_config_payload,
)
from loops.task_providers.registry import get_provider_definition
from loops.state.run_record import RunRecord, Task, write_run_record
from loops.task_providers.base import TaskProvider


class SyncModeInterruptedError(KeyboardInterrupt):
    """Raised when sync-mode inner-loop execution is interrupted."""

    def __init__(self, *, run_dir: Path) -> None:
        super().__init__()
        self.run_dir = run_dir


@dataclass(frozen=True)
class OuterLoopConfig:
    """Configuration for the outer loop polling and dispatch behavior."""

    poll_interval_seconds: int = 30
    parallel_tasks: bool = False
    parallel_tasks_limit: int = 5
    sync_mode: bool = False
    emit_on_first_run: bool = False
    force: bool = False
    task_ready_status: str = "Ready"
    auto_approve_enabled: bool = False
    handoff_handler: str = DEFAULT_HANDOFF_HANDLER
    checkout_mode: str = CHECKOUT_MODE_BRANCH


@dataclass(frozen=True)
class InnerLoopCommandConfig:
    """Configuration for launching inner loop commands."""

    command: list[str]
    working_dir: Optional[str] = None
    env: Optional[dict[str, str]] = None
    append_task_url: bool = True

    @staticmethod
    def from_dict(
        payload: dict[str, Any],
        *,
        base_dir: Optional[Path] = None,
    ) -> "InnerLoopCommandConfig":
        """Build an InnerLoopCommandConfig from a dict payload."""

        if "command" not in payload:
            raise KeyError("inner_loop.command is required")
        raw_command = payload["command"]
        if isinstance(raw_command, str):
            command = shlex.split(raw_command)
        elif isinstance(raw_command, list) and all(
            isinstance(item, str) for item in raw_command
        ):
            command = list(raw_command)
        else:
            raise TypeError("inner_loop.command must be a string or list of strings")
        if not command:
            raise ValueError("inner_loop.command cannot be empty")
        working_dir = payload.get("working_dir")
        if working_dir is not None and not isinstance(working_dir, str):
            raise TypeError("inner_loop.working_dir must be a string")
        if working_dir is not None and base_dir is not None:
            working_path = Path(working_dir)
            if not working_path.is_absolute():
                working_dir = str((base_dir / working_path).resolve())
        env = payload.get("env")
        if env is not None:
            if not isinstance(env, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in env.items()
            ):
                raise TypeError("inner_loop.env must be a string-to-string map")
        append_task_url = payload.get("append_task_url", True)
        if not isinstance(append_task_url, bool):
            raise TypeError("inner_loop.append_task_url must be a boolean")
        return InnerLoopCommandConfig(
            command=command,
            working_dir=working_dir,
            env=env,
            append_task_url=append_task_url,
        )


@dataclass(frozen=True)
class LoopsConfig:
    """Top-level Loops configuration loaded from JSON."""

    version: int
    task_provider_id: str
    task_provider_config: dict[str, Any]
    loop_config: OuterLoopConfig
    inner_loop: Optional[InnerLoopCommandConfig] = None


@dataclass
class OuterLoopState:
    """Persisted outer loop state used for deduplication."""

    initialized: bool
    tasks: dict[str, dict[str, Any]]
    updated_at: str

    @staticmethod
    def empty() -> "OuterLoopState":
        """Return an empty state snapshot."""

        return OuterLoopState(initialized=False, tasks={}, updated_at=_now_iso())

    def has_task(self, task: Task) -> bool:
        """Return True if the task has been seen before."""

        return _task_key(task) in self.tasks

    def record_task(self, task: Task, now_iso: str) -> None:
        """Record a task in the state ledger."""

        key = _task_key(task)
        entry = self.tasks.get(key)
        if entry is None:
            entry = {"first_seen_at": now_iso}
        entry["task"] = task.to_dict()
        entry["last_seen_at"] = now_iso
        self.tasks[key] = entry


class OuterLoopRunner:
    """Run the outer loop to poll providers and start inner loops."""

    def __init__(
        self,
        provider: TaskProvider,
        config: OuterLoopConfig,
        *,
        loops_root: Path,
        inner_loop_launcher: Optional[Callable[[Path, Task], None]] = None,
    ) -> None:
        self.provider = provider
        self.config = config
        self.loops_root = loops_root
        self.inner_loop_launcher = inner_loop_launcher
        self.state_path = self.loops_root / OUTER_STATE_FILE_NAME
        self.log_path = self.loops_root / OUTER_LOG_FILE_NAME

    def run_once(
        self,
        limit: int | None = None,
        *,
        forced_task_url: str | None = None,
    ) -> list[Path]:
        """Run a single poll cycle and return created run directories."""

        self.loops_root.mkdir(parents=True, exist_ok=True)
        _inner_loop_runs_root(self.loops_root).mkdir(parents=True, exist_ok=True)
        state = read_outer_state(self.state_path)
        first_run = not state.initialized
        poll_limit = None if forced_task_url is not None else limit
        _log(
            self.log_path,
            "run_once.start "
            f"first_run={first_run} "
            f"poll_limit={poll_limit if poll_limit is not None else 'none'} "
            f"forced_task_url={'set' if forced_task_url is not None else 'none'} "
            f"emit_on_first_run={self.config.emit_on_first_run} "
            f"force={self.config.force} "
            f"ready_status={self.config.task_ready_status!r} "
            f"checkout_mode={self.config.checkout_mode}",
            stream_to_stdout=self.config.sync_mode,
        )

        polled_tasks = self.provider.poll(poll_limit)
        if forced_task_url is not None:
            selected_task = _select_task_by_url(polled_tasks, forced_task_url)
            ready_tasks = [selected_task]
            _log(
                self.log_path,
                "run_once.forced_task_selected "
                f"key={_task_key(selected_task)} "
                f"url={selected_task.url}",
                stream_to_stdout=self.config.sync_mode,
            )
        else:
            ready_tasks = [task for task in polled_tasks if _is_ready(task, self.config)]

        _log(
            self.log_path,
            "run_once.poll "
            f"polled={len(polled_tasks)} "
            f"ready={len(ready_tasks)}",
            stream_to_stdout=self.config.sync_mode,
        )
        now_iso = _now_iso()
        emit_tasks: list[Task] = []
        should_emit = self.config.emit_on_first_run or self.config.force or not first_run
        seen_count = 0
        skipped_not_emitting_count = 0
        skipped_seen_count = 0

        for task in ready_tasks:
            already_seen = state.has_task(task)
            if already_seen:
                seen_count += 1
            state.record_task(task, now_iso)
            if not should_emit:
                skipped_not_emitting_count += 1
                continue
            if already_seen and not self.config.force:
                skipped_seen_count += 1
                continue
            emit_tasks.append(task)

        _log(
            self.log_path,
            "run_once.select "
            f"should_emit={should_emit} "
            f"seen={seen_count} "
            f"skipped_not_emitting={skipped_not_emitting_count} "
            f"skipped_seen={skipped_seen_count} "
            f"emit={len(emit_tasks)}",
            stream_to_stdout=self.config.sync_mode,
        )

        if emit_tasks and self.inner_loop_launcher is None:
            _log(
                self.log_path,
                "run_once.error reason=missing_inner_loop_launcher",
                stream_to_stdout=self.config.sync_mode,
            )
            raise RuntimeError("inner_loop_launcher is required to launch tasks")
        if not emit_tasks:
            _log(
                self.log_path,
                "no task ready to be scheduled",
                stream_to_stdout=self.config.sync_mode,
            )
        starting_commit = (
            _resolve_starting_commit(self.loops_root) if emit_tasks else "unknown"
        )
        to_launch: list[tuple[Path, Task]] = []
        for task in emit_tasks:
            run_dir = create_run_dir(task, self.loops_root)
            _log(
                self.log_path,
                "run_once.schedule "
                f"key={_task_key(task)} "
                f"url={task.url} "
                f"run_dir={run_dir} "
                f"checkout_mode={self.config.checkout_mode} "
                f"starting_commit={starting_commit}",
                stream_to_stdout=self.config.sync_mode,
            )
            record = RunRecord(
                task=task,
                pr=None,
                codex_session=None,
                needs_user_input=False,
                stream_logs_stdout=self.config.sync_mode,
                checkout_mode=self.config.checkout_mode,
                starting_commit=starting_commit,
                last_state="RUNNING",
                updated_at=now_iso,
            )
            write_run_record(run_dir / RUN_RECORD_FILE_NAME, record)
            _touch(run_dir / RUN_LOG_FILE_NAME)
            _touch(run_dir / AGENT_LOG_FILE_NAME)
            to_launch.append((run_dir, task))

        _log(
            self.log_path,
            "run_once.launch "
            f"prepared={len(to_launch)} "
            f"tasks={_task_keys_preview([task for _, task in to_launch])}",
            stream_to_stdout=self.config.sync_mode,
        )

        launch_error: str | None = None
        try:
            if to_launch:
                _log(
                    self.log_path,
                    "run_once.launching "
                    f"count={len(to_launch)} "
                    f"tasks={_task_keys_preview([task for _, task in to_launch])}",
                    stream_to_stdout=self.config.sync_mode,
                )
                self._launch_tasks(to_launch)
        except Exception as exc:
            launch_error = type(exc).__name__
            raise
        finally:
            state.initialized = True
            state.updated_at = now_iso
            write_outer_state(self.state_path, state)
            _log(
                self.log_path,
                _format_log_line(len(ready_tasks), len(to_launch)),
                stream_to_stdout=self.config.sync_mode,
            )
            _log(
                self.log_path,
                "run_once.done "
                f"state_tasks={len(state.tasks)} "
                f"launch_error={launch_error or 'none'}",
                stream_to_stdout=self.config.sync_mode,
            )
        return [run_dir for run_dir, _ in to_launch]

    def run_forever(self, limit: int | None = None) -> None:
        """Run poll cycles forever until interrupted."""

        while True:
            self.run_once(limit=limit)
            time.sleep(self.config.poll_interval_seconds)

    def _launch_tasks(self, tasks: list[tuple[Path, Task]]) -> None:
        """Launch tasks sequentially or in parallel based on config."""

        launcher = self.inner_loop_launcher
        if launcher is None:
            raise RuntimeError("inner_loop_launcher is required to launch tasks")
        if self.config.sync_mode:
            # Foreground mode is explicitly interactive; launch serially so stdin/stdout
            # are unambiguous and user handoff prompts are readable.
            for run_dir, task in tasks:
                launcher(run_dir, task)
            return
        if not self.config.parallel_tasks or len(tasks) <= 1:
            for run_dir, task in tasks:
                launcher(run_dir, task)
            return

        max_workers = min(self.config.parallel_tasks_limit, len(tasks))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(launcher, run_dir, task)
                for run_dir, task in tasks
            ]
            for future in futures:
                future.result()


def load_config(path: str | Path) -> LoopsConfig:
    """Load a LoopsConfig from a JSON file."""

    payload = json.loads(Path(path).read_text())
    payload, _ = upgrade_config_payload(payload)
    task_provider_id = payload.get("task_provider_id")
    if not isinstance(task_provider_id, str) or not task_provider_id:
        raise TypeError("task_provider_id is required and must be a string")
    task_provider_config = payload.get("task_provider_config")
    if not isinstance(task_provider_config, dict):
        raise TypeError("task_provider_config must be an object")
    loop_config = _load_outer_loop_config(payload.get("loop_config"))
    validate_handoff_handler_provider_compatibility(
        loop_config.handoff_handler,
        task_provider_id,
    )
    inner_loop_payload = payload.get("inner_loop")
    inner_loop = None
    if inner_loop_payload is not None:
        if not isinstance(inner_loop_payload, dict):
            raise TypeError("inner_loop must be an object")
        base_dir = Path(path).resolve().parent
        inner_loop = InnerLoopCommandConfig.from_dict(
            inner_loop_payload,
            base_dir=base_dir,
        )
    return LoopsConfig(
        version=_load_config_version(payload),
        task_provider_id=task_provider_id,
        task_provider_config=task_provider_config,
        loop_config=loop_config,
        inner_loop=inner_loop,
    )


def upgrade_config_payload(payload: Any) -> tuple[dict[str, Any], bool]:
    """Upgrade a config payload in-memory to the latest schema version."""

    if not isinstance(payload, dict):
        raise TypeError("Config must be a JSON object")
    upgraded = dict(payload)
    changed = False
    version = _load_config_version(upgraded)

    if version > LATEST_LOOPS_CONFIG_VERSION:
        raise ValueError(
            f"Unsupported config version: {version}. "
            f"Latest supported version is {LATEST_LOOPS_CONFIG_VERSION}."
        )

    if version < LATEST_LOOPS_CONFIG_VERSION:
        upgraded["version"] = LATEST_LOOPS_CONFIG_VERSION
        changed = True

    legacy_provider_id = upgraded.pop("provider_id", None)
    if legacy_provider_id is not None:
        changed = True
        if "task_provider_id" not in upgraded:
            upgraded["task_provider_id"] = legacy_provider_id

    legacy_provider_config = upgraded.pop("provider_config", None)
    if legacy_provider_config is not None:
        changed = True
        if "task_provider_config" not in upgraded:
            upgraded["task_provider_config"] = legacy_provider_config

    existing_loop_payload = upgraded.get("loop_config")
    if existing_loop_payload is None:
        loop_payload: dict[str, Any] = {}
    elif isinstance(existing_loop_payload, dict):
        loop_payload = dict(existing_loop_payload)
    else:
        raise TypeError("loop_config must be an object")

    legacy_approval_comment_usernames = loop_payload.pop(
        "approval_comment_usernames",
        None,
    )
    if legacy_approval_comment_usernames is not None:
        changed = True
    legacy_approval_comment_pattern = loop_payload.pop(
        "approval_comment_pattern",
        None,
    )
    if legacy_approval_comment_pattern is not None:
        changed = True

    for key, value in build_default_loop_config_payload().items():
        if key not in loop_payload:
            loop_payload[key] = value
            changed = True

    if existing_loop_payload != loop_payload:
        upgraded["loop_config"] = loop_payload

    task_provider_id = upgraded.get("task_provider_id")
    if task_provider_id == GITHUB_PROJECTS_V2_PROVIDER_ID:
        existing_provider_payload = upgraded.get("task_provider_config")
        if existing_provider_payload is None:
            provider_payload: dict[str, Any] = {}
        elif isinstance(existing_provider_payload, dict):
            provider_payload = dict(existing_provider_payload)
        else:
            raise TypeError("task_provider_config must be an object")
        project_url = provider_payload.get("url")
        if not isinstance(project_url, str) or not project_url.strip():
            provider_defaults = build_default_provider_config_payload()
        else:
            provider_defaults = build_default_provider_config_payload(
                project_url=project_url,
            )
        for key, value in provider_defaults.items():
            if key == "url":
                continue
            if key not in provider_payload:
                if (
                    key == "approval_comment_usernames"
                    and legacy_approval_comment_usernames is not None
                ):
                    provider_payload[key] = legacy_approval_comment_usernames
                elif (
                    key == "approval_comment_pattern"
                    and legacy_approval_comment_pattern is not None
                ):
                    provider_payload[key] = legacy_approval_comment_pattern
                else:
                    provider_payload[key] = value
                changed = True
        if existing_provider_payload != provider_payload:
            upgraded["task_provider_config"] = provider_payload

    return upgraded, changed


def build_provider(config: LoopsConfig) -> TaskProvider:
    """Construct the task provider for the configured provider id."""

    definition = get_provider_definition(config.task_provider_id)
    _validate_required_secrets(definition.metadata, environ=os.environ)
    try:
        provider_config = definition.metadata.provider_config_model.model_validate(
            config.task_provider_config
        )
    except ValidationError as exc:
        raise ValueError(
            f"task_provider_config is invalid for provider "
            f"'{definition.metadata.id}': {exc}"
        ) from exc
    return definition.build(provider_config)


def _resolve_provider_review_actor_usernames(provider: TaskProvider) -> tuple[str, ...]:
    """Resolve an optional provider-defined review actor allowlist."""

    raw_usernames = getattr(provider, "review_actor_allowlist", ())
    if isinstance(raw_usernames, tuple):
        usernames = raw_usernames
    elif isinstance(raw_usernames, list):
        usernames = tuple(raw_usernames)
    else:
        return ()
    if not all(isinstance(item, str) for item in usernames):
        return ()
    return normalize_approval_usernames(usernames)


def _resolve_provider_comment_approval_usernames(
    provider: TaskProvider,
) -> tuple[str, ...]:
    """Resolve provider-defined comment approval usernames."""

    raw_usernames = getattr(provider, "approval_comment_usernames", ())
    if isinstance(raw_usernames, tuple):
        usernames = raw_usernames
    elif isinstance(raw_usernames, list):
        usernames = tuple(raw_usernames)
    else:
        return ()
    if not all(isinstance(item, str) for item in usernames):
        return ()
    return normalize_approval_usernames(usernames)


def _resolve_provider_comment_approval_pattern(provider: TaskProvider) -> str:
    """Resolve provider-defined approval regex pattern."""

    raw_pattern = getattr(provider, "approval_comment_pattern", "")
    if not isinstance(raw_pattern, str):
        return DEFAULT_APPROVAL_COMMENT_PATTERN
    return raw_pattern or DEFAULT_APPROVAL_COMMENT_PATTERN


def build_inner_loop_launcher(
    config: LoopsConfig,
    *,
    review_actor_usernames: tuple[str, ...] = (),
    approval_comment_usernames: tuple[str, ...] = (),
    approval_comment_pattern: str = DEFAULT_APPROVAL_COMMENT_PATTERN,
) -> Callable[[Path, Task], None]:
    """Build the launcher callable for inner loop executions."""

    if config.inner_loop is None:
        raise ValueError("inner_loop.command is required to launch tasks")
    inner_loop = config.inner_loop
    sync_mode = config.loop_config.sync_mode
    normalized_review_actor_usernames = normalize_approval_usernames(
        review_actor_usernames
    )
    normalized_approval_comment_usernames = normalize_approval_usernames(
        approval_comment_usernames
    )
    resolved_approval_comment_pattern = (
        approval_comment_pattern or DEFAULT_APPROVAL_COMMENT_PATTERN
    )

    def launcher(run_dir: Path, task: Task) -> None:
        """Launch a single inner loop invocation."""

        run_dir.mkdir(parents=True, exist_ok=True)
        run_log = run_dir / RUN_LOG_FILE_NAME
        write_inner_loop_runtime_config(
            run_dir,
            InnerLoopRuntimeConfig(
                handoff_handler=config.loop_config.handoff_handler,
                auto_approve_enabled=config.loop_config.auto_approve_enabled,
                stream_logs_stdout=sync_mode,
                env=dict(inner_loop.env) if inner_loop.env else None,
                approval_comment_usernames=normalized_approval_comment_usernames,
                approval_comment_pattern=resolved_approval_comment_pattern,
                review_actor_usernames=normalized_review_actor_usernames,
            ),
        )
        env = os.environ.copy()
        env["LOOPS_RUN_DIR"] = str(run_dir)
        command = list(inner_loop.command)
        launches_loops_inner_loop = _is_loops_inner_loop_command(command)
        if inner_loop.env and not launches_loops_inner_loop:
            env.update(inner_loop.env)
        if inner_loop.append_task_url:
            command.append(task.url)

        if sync_mode:
            try:
                subprocess.run(
                    command,
                    cwd=inner_loop.working_dir,
                    env=env,
                    check=False,
                )
            except KeyboardInterrupt as exc:
                raise SyncModeInterruptedError(run_dir=run_dir) from exc
            return

        log_fd = os.open(str(run_log), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            subprocess.Popen(
                command,
                cwd=inner_loop.working_dir,
                stdout=log_fd,
                stderr=subprocess.STDOUT,
                env=env,
            )
        finally:
            os.close(log_fd)

    return launcher


def _is_loops_inner_loop_command(command: list[str]) -> bool:
    if not command:
        return False
    for index, item in enumerate(command):
        name = Path(item).name.casefold()
        if name == "loops" and index + 1 < len(command):
            if command[index + 1].casefold() == "inner-loop":
                return True
        if item == "-m" and index + 1 < len(command):
            if command[index + 1].casefold() == "loops" and index + 2 < len(command):
                if command[index + 2].casefold() == "inner-loop":
                    return True
    return False


def read_outer_state(path: str | Path) -> OuterLoopState:
    """Read the outer state ledger from disk or return empty."""

    target = Path(path)
    if not target.exists():
        return OuterLoopState.empty()
    payload = json.loads(target.read_text())
    if not isinstance(payload, dict):
        raise TypeError("outer_state.json must contain a JSON object")
    initialized = payload.get("initialized", False)
    if not isinstance(initialized, bool):
        raise TypeError("outer_state.json initialized must be a boolean")
    tasks = payload.get("tasks") or {}
    if not isinstance(tasks, dict):
        raise TypeError("outer_state.json tasks must be an object")
    updated_at = payload.get("updated_at") or _now_iso()
    return OuterLoopState(initialized=initialized, tasks=tasks, updated_at=str(updated_at))


def write_outer_state(path: str | Path, state: OuterLoopState) -> None:
    """Persist outer state to disk."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "initialized": state.initialized,
        "tasks": state.tasks,
        "updated_at": state.updated_at,
    }
    target.write_text(json.dumps(payload, indent=2, sort_keys=True))


def create_run_dir(task: Task, loops_root: Path) -> Path:
    """Create a run directory for a task and return its path."""

    runs_root = _inner_loop_runs_root(loops_root)
    runs_root.mkdir(parents=True, exist_ok=True)
    date_prefix = datetime.now(timezone.utc).date().isoformat()
    title_slug = _slugify(task.title) or "task"
    id_slug = _slugify(task.id) or "id"
    base_name = f"{date_prefix}-{title_slug}-{id_slug}"
    candidate = runs_root / base_name
    if not candidate.exists():
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate
    suffix = 1
    while True:
        contender = runs_root / f"{base_name}-{suffix}"
        if not contender.exists():
            contender.mkdir(parents=True, exist_ok=True)
            return contender
        suffix += 1


def _inner_loop_runs_root(loops_root: Path) -> Path:
    """Return the directory containing per-task inner-loop run directories."""

    return loops_root / INNER_LOOP_RUNS_DIR_NAME


def _load_outer_loop_config(payload: Any) -> OuterLoopConfig:
    """Load outer loop configuration from JSON payload."""

    if payload is None:
        raw_payload: dict[str, Any] = {}
    elif isinstance(payload, dict):
        raw_payload = payload
    else:
        raise TypeError("loop_config must be an object")
    defaults = build_default_loop_config_payload()
    merged_payload = {**defaults, **raw_payload}
    poll_interval = _load_int(
        merged_payload,
        "poll_interval_seconds",
        defaults["poll_interval_seconds"],
    )
    if poll_interval <= 0:
        raise ValueError("poll_interval_seconds must be positive")
    parallel_limit = _load_int(
        merged_payload,
        "parallel_tasks_limit",
        defaults["parallel_tasks_limit"],
    )
    if parallel_limit <= 0:
        raise ValueError("parallel_tasks_limit must be positive")
    return OuterLoopConfig(
        poll_interval_seconds=poll_interval,
        parallel_tasks=_load_bool(
            merged_payload,
            "parallel_tasks",
            defaults["parallel_tasks"],
        ),
        parallel_tasks_limit=parallel_limit,
        sync_mode=_load_bool(merged_payload, "sync_mode", defaults["sync_mode"]),
        emit_on_first_run=_load_bool(
            merged_payload,
            "emit_on_first_run",
            defaults["emit_on_first_run"],
        ),
        force=_load_bool(merged_payload, "force", defaults["force"]),
        task_ready_status=_load_str(
            merged_payload,
            "task_ready_status",
            defaults["task_ready_status"],
        ),
        auto_approve_enabled=_load_bool(
            merged_payload,
            "auto_approve_enabled",
            defaults["auto_approve_enabled"],
        ),
        handoff_handler=validate_handoff_handler_name(
            _load_str(
                merged_payload,
                "handoff_handler",
                defaults["handoff_handler"],
            )
        ),
        checkout_mode=_load_checkout_mode(
            merged_payload,
            "checkout_mode",
            defaults["checkout_mode"],
        ),
    )


def build_default_loop_config_payload() -> dict[str, Any]:
    """Build the canonical loop_config defaults payload for JSON config files."""

    defaults = OuterLoopConfig()
    return {
        "poll_interval_seconds": defaults.poll_interval_seconds,
        "parallel_tasks": defaults.parallel_tasks,
        "parallel_tasks_limit": defaults.parallel_tasks_limit,
        "sync_mode": defaults.sync_mode,
        "emit_on_first_run": defaults.emit_on_first_run,
        "force": defaults.force,
        "task_ready_status": defaults.task_ready_status,
        "auto_approve_enabled": defaults.auto_approve_enabled,
        "handoff_handler": defaults.handoff_handler,
        "checkout_mode": defaults.checkout_mode,
    }


def _validate_required_secrets(
    provider: LoopsProviderConfig,
    *,
    environ: Mapping[str, str],
) -> None:
    """Validate that provider-declared env secrets are present."""

    missing: list[SecretRequirement] = []
    for requirement in provider.required_secrets:
        if _resolve_secret_env_name(requirement, environ=environ) is None:
            missing.append(requirement)
    if not missing:
        return

    lines = [
        f"Missing required secret environment variables for provider "
        f"'{provider.display_name()}' ({provider.id}):"
    ]
    for requirement in missing:
        env_names = ", ".join(requirement.env_names()) or requirement.name
        lines.append(f"- {env_names}: {requirement.description}")
    raise ValueError("\n".join(lines))


def _resolve_secret_env_name(
    requirement: SecretRequirement,
    *,
    environ: Mapping[str, str],
) -> str | None:
    """Resolve the first non-empty env var that satisfies a secret requirement."""

    for env_name in requirement.env_names():
        value = environ.get(env_name)
        if value is not None and value.strip():
            return env_name
    return None


def _load_bool(payload: dict[str, Any], key: str, default: bool) -> bool:
    """Load a boolean config value with validation."""

    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise TypeError(f"{key} must be a boolean")
    return value


def _load_int(payload: dict[str, Any], key: str, default: int) -> int:
    """Load an integer config value with validation."""

    value = payload.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{key} must be an integer")
    return value


def _load_str(payload: dict[str, Any], key: str, default: str) -> str:
    """Load a string config value with validation."""

    value = payload.get(key, default)
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string")
    return value


def _load_checkout_mode(payload: dict[str, Any], key: str, default: str) -> str:
    """Load and validate checkout mode."""

    checkout_mode = _load_str(payload, key, default).strip().casefold()
    if checkout_mode not in VALID_CHECKOUT_MODES:
        raise ValueError(f"{key} must be one of: branch, worktree")
    return checkout_mode


def _load_config_version(payload: dict[str, Any]) -> int:
    value = payload.get("version", 0)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("version must be an integer")
    if value < 0:
        raise ValueError("version must be >= 0")
    return value


def _is_ready(task: Task, config: OuterLoopConfig) -> bool:
    """Return True if a task matches the configured ready status."""

    return task.status.casefold() == config.task_ready_status.casefold()


def _select_task_by_url(tasks: list[Task], task_url: str) -> Task:
    """Select a single task by URL after normalizing common URL variants."""

    normalized_target = _normalize_task_url(task_url)
    matches: list[Task] = []
    seen_keys: set[str] = set()
    for task in tasks:
        try:
            normalized_task_url = _normalize_task_url(task.url)
        except ValueError:
            continue
        if normalized_task_url != normalized_target:
            continue
        key = _task_key(task)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        matches.append(task)

    if not matches:
        raise ValueError(
            f"--task-url was not found in provider results: {task_url}"
        )
    if len(matches) > 1:
        candidates = ", ".join(
            f"{task.id} ({task.url})" for task in matches[:5]
        )
        raise ValueError(
            f"--task-url matched multiple tasks: {task_url}. Candidates: {candidates}"
        )
    return matches[0]


def _normalize_task_url(url: str) -> str:
    raw_url = url.strip()
    if not raw_url:
        raise ValueError("task URL cannot be empty")
    parts = urlsplit(raw_url)
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"task URL must be absolute: {url}")
    normalized_path = parts.path.rstrip("/") or "/"
    return urlunsplit(
        (
            parts.scheme.casefold(),
            parts.netloc.casefold(),
            normalized_path,
            "",
            "",
        )
    )


def _task_key(task: Task) -> str:
    """Return the dedupe key for a task."""

    return f"{task.provider_id}:{task.id}"


def _slugify(value: str) -> str:
    """Normalize a string for filesystem-friendly directory names."""

    normalized = value.lower()
    normalized = re.sub(r"[^a-z0-9]+", "-", normalized)
    normalized = normalized.strip("-")
    normalized = re.sub(r"-+", "-", normalized)
    return normalized[:60]


def _now_iso() -> str:
    """Return the current time as ISO-8601 UTC string."""

    return datetime.now(timezone.utc).isoformat()


def _log(
    path: Path,
    message: str,
    *,
    stream_to_stdout: bool = False,
) -> None:
    """Append a log message to the outer loop log."""

    path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = format_log_timestamp()
    rendered_line = f"{timestamp} {message}"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{rendered_line}\n")

    if stream_to_stdout:
        print(rendered_line, flush=True)


def _format_log_line(ready_count: int, processed_count: int) -> str:
    """Format a log line for the outer loop runner."""

    return f"ready={ready_count} processed={processed_count}"


def _task_keys_preview(tasks: list[Task], *, max_items: int = 5) -> str:
    """Return a compact preview of task keys for logging."""

    if not tasks:
        return "-"
    keys = [_task_key(task) for task in tasks[:max_items]]
    preview = ",".join(keys)
    if len(tasks) > max_items:
        preview = f"{preview},+{len(tasks) - max_items}more"
    return preview


def _touch(path: Path) -> None:
    """Ensure a file exists on disk."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)


def _resolve_starting_commit(loops_root: Path) -> str:
    """Resolve git HEAD for the workspace being orchestrated."""

    repo_root = loops_root.resolve().parent
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    commit = completed.stdout.strip()
    return commit or "unknown"
