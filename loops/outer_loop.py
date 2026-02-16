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
from typing import Any, Callable, Optional
from urllib.parse import urlsplit, urlunsplit

from loops.providers.github_projects_v2 import (
    GITHUB_PROJECTS_V2_PROVIDER_ID,
    GithubProjectsV2TaskProvider,
    GithubProjectsV2TaskProviderConfig,
)
from loops.run_record import RunRecord, Task, write_run_record
from loops.task_provider import TaskProvider

DEFAULT_POLL_INTERVAL_SECONDS = 30
DEFAULT_PARALLEL_TASKS_LIMIT = 5
DEFAULT_TASK_READY_STATUS = "Ready"
INNER_LOOP_RUNS_DIR_NAME = "jobs"


@dataclass(frozen=True)
class OuterLoopConfig:
    """Configuration for the outer loop polling and dispatch behavior."""

    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS
    parallel_tasks: bool = False
    parallel_tasks_limit: int = DEFAULT_PARALLEL_TASKS_LIMIT
    sync_mode: bool = False
    emit_on_first_run: bool = False
    force: bool = False
    task_ready_status: str = DEFAULT_TASK_READY_STATUS


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

    provider_id: str
    provider_config: dict[str, Any]
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
        self.state_path = self.loops_root / "outer_state.json"
        self.log_path = self.loops_root / "oloops.log"

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
        poll_limit = None if forced_task_url is not None else limit
        polled_tasks = self.provider.poll(poll_limit)
        if forced_task_url is not None:
            ready_tasks = [_select_task_by_url(polled_tasks, forced_task_url)]
        else:
            ready_tasks = [task for task in polled_tasks if _is_ready(task, self.config)]
        now_iso = _now_iso()
        emit_tasks: list[Task] = []
        first_run = not state.initialized
        should_emit = self.config.emit_on_first_run or self.config.force or not first_run

        for task in ready_tasks:
            already_seen = state.has_task(task)
            state.record_task(task, now_iso)
            if not should_emit:
                continue
            if already_seen and not self.config.force:
                continue
            emit_tasks.append(task)

        if emit_tasks and self.inner_loop_launcher is None:
            raise RuntimeError("inner_loop_launcher is required to launch tasks")
        to_launch: list[tuple[Path, Task]] = []
        for task in emit_tasks:
            run_dir = create_run_dir(task, self.loops_root)
            record = RunRecord(
                task=task,
                pr=None,
                codex_session=None,
                needs_user_input=False,
                last_state="RUNNING",
                updated_at=now_iso,
            )
            write_run_record(run_dir / "run.json", record)
            _touch(run_dir / "run.log")
            _touch(run_dir / "agent.log")
            to_launch.append((run_dir, task))

        try:
            if to_launch:
                self._launch_tasks(to_launch)
        finally:
            state.initialized = True
            state.updated_at = now_iso
            write_outer_state(self.state_path, state)
            _log(self.log_path, _format_log_line(len(ready_tasks), len(to_launch)))
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
    if not isinstance(payload, dict):
        raise TypeError("Config must be a JSON object")
    provider_id = payload.get("provider_id")
    if not isinstance(provider_id, str) or not provider_id:
        raise TypeError("provider_id is required and must be a string")
    provider_config = payload.get("provider_config")
    if not isinstance(provider_config, dict):
        raise TypeError("provider_config must be an object")
    loop_config = _load_outer_loop_config(payload.get("loop_config"))
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
        provider_id=provider_id,
        provider_config=provider_config,
        loop_config=loop_config,
        inner_loop=inner_loop,
    )


def build_provider(config: LoopsConfig) -> TaskProvider:
    """Construct the task provider for the configured provider id."""

    if config.provider_id != GITHUB_PROJECTS_V2_PROVIDER_ID:
        raise ValueError(f"Unsupported provider_id: {config.provider_id}")
    provider_kwargs = _filter_provider_config(config.provider_config)
    return GithubProjectsV2TaskProvider(GithubProjectsV2TaskProviderConfig(**provider_kwargs))


def build_inner_loop_launcher(
    config: LoopsConfig,
) -> Callable[[Path, Task], None]:
    """Build the launcher callable for inner loop executions."""

    if config.inner_loop is None:
        raise ValueError("inner_loop.command is required to launch tasks")
    inner_loop = config.inner_loop
    sync_mode = config.loop_config.sync_mode

    def launcher(run_dir: Path, task: Task) -> None:
        """Launch a single inner loop invocation."""

        run_dir.mkdir(parents=True, exist_ok=True)
        run_log = run_dir / "run.log"
        env = os.environ.copy()
        env["LOOPS_RUN_DIR"] = str(run_dir)
        env["LOOPS_TASK_ID"] = task.id
        env["LOOPS_TASK_TITLE"] = task.title
        env["LOOPS_TASK_URL"] = task.url
        env["LOOPS_TASK_PROVIDER"] = task.provider_id
        if inner_loop.env:
            env.update(inner_loop.env)
        command = list(inner_loop.command)
        if inner_loop.append_task_url:
            command.append(task.url)

        if sync_mode:
            subprocess.run(
                command,
                cwd=inner_loop.working_dir,
                env=env,
                check=False,
            )
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
        return OuterLoopConfig()
    if not isinstance(payload, dict):
        raise TypeError("loop_config must be an object")
    poll_interval = _load_int(payload, "poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS)
    if poll_interval <= 0:
        raise ValueError("poll_interval_seconds must be positive")
    parallel_limit = _load_int(payload, "parallel_tasks_limit", DEFAULT_PARALLEL_TASKS_LIMIT)
    if parallel_limit <= 0:
        raise ValueError("parallel_tasks_limit must be positive")
    return OuterLoopConfig(
        poll_interval_seconds=poll_interval,
        parallel_tasks=_load_bool(payload, "parallel_tasks", False),
        parallel_tasks_limit=parallel_limit,
        sync_mode=_load_bool(payload, "sync_mode", False),
        emit_on_first_run=_load_bool(payload, "emit_on_first_run", False),
        force=_load_bool(payload, "force", False),
        task_ready_status=_load_str(payload, "task_ready_status", DEFAULT_TASK_READY_STATUS),
    )


def _filter_provider_config(payload: dict[str, Any]) -> dict[str, Any]:
    """Filter provider config and reject unknown keys."""

    allowed_keys = {"url", "status_field", "page_size", "github_token"}
    unknown_keys = set(payload.keys()) - allowed_keys
    if unknown_keys:
        formatted = ", ".join(sorted(unknown_keys))
        raise ValueError(f"provider_config contains unsupported keys: {formatted}")
    return dict(payload)


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


def _log(path: Path, message: str) -> None:
    """Append a log message to the outer loop log."""

    path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = _now_iso()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{timestamp} {message}\n")


def _format_log_line(ready_count: int, processed_count: int) -> str:
    """Format a log line for the outer loop runner."""

    return f"ready={ready_count} processed={processed_count}"


def _touch(path: Path) -> None:
    """Ensure a file exists on disk."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
