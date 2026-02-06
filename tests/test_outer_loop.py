from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from loops.outer_loop import OuterLoopConfig, OuterLoopRunner, load_config, read_outer_state
from loops.run_record import Task, read_run_record


class StubProvider:
    def __init__(self, tasks: list[Task]) -> None:
        self._tasks = list(tasks)

    def poll(self, limit: int | None = None) -> list[Task]:
        if limit is None:
            return list(self._tasks)
        return list(self._tasks)[:limit]


def make_task(task_id: str, title: str, status: str = "Ready") -> Task:
    return Task(
        provider_id="stub",
        id=task_id,
        title=title,
        status=status,
        url=f"https://example.com/{task_id}",
        created_at="2026-02-05T00:00:00Z",
        updated_at="2026-02-05T00:00:00Z",
    )


def list_run_dirs(loops_root: Path) -> list[Path]:
    if not loops_root.exists():
        return []
    return sorted([path for path in loops_root.iterdir() if path.is_dir()])


def test_run_once_creates_run_records(tmp_path: Path) -> None:
    tasks = [make_task("1", "Ship it"), make_task("2", "Next")]
    provider = StubProvider(tasks)
    loops_root = tmp_path / ".loops"
    launched: list[Path] = []

    def launcher(run_dir: Path, _task: Task) -> None:
        launched.append(run_dir)

    config = OuterLoopConfig(task_ready_status="Ready", emit_on_first_run=True)
    runner = OuterLoopRunner(
        provider, config, loops_root=loops_root, inner_loop_launcher=launcher
    )

    runner.run_once()

    run_dirs = list_run_dirs(loops_root)
    assert len(run_dirs) == 2
    titles = {read_run_record(run_dir / "run.json").task.title for run_dir in run_dirs}
    assert titles == {"Ship it", "Next"}
    assert len(launched) == 2

    state = read_outer_state(loops_root / "outer_state.json")
    assert state.initialized is True
    assert len(state.tasks) == 2


def test_run_once_dedupes_without_force(tmp_path: Path) -> None:
    tasks = [make_task("1", "Ship it"), make_task("2", "Next")]
    provider = StubProvider(tasks)
    loops_root = tmp_path / ".loops"
    launched: list[Path] = []

    def launcher(run_dir: Path, _task: Task) -> None:
        launched.append(run_dir)

    config = OuterLoopConfig(task_ready_status="Ready", emit_on_first_run=True)
    runner = OuterLoopRunner(
        provider, config, loops_root=loops_root, inner_loop_launcher=launcher
    )

    runner.run_once()
    initial_dirs = list_run_dirs(loops_root)
    runner.run_once()

    assert list_run_dirs(loops_root) == initial_dirs
    assert len(launched) == 2


def test_force_reprocesses_tasks(tmp_path: Path) -> None:
    tasks = [make_task("1", "Ship it"), make_task("2", "Next")]
    provider = StubProvider(tasks)
    loops_root = tmp_path / ".loops"
    launched: list[Path] = []

    def launcher(run_dir: Path, _task: Task) -> None:
        launched.append(run_dir)

    config = OuterLoopConfig(task_ready_status="Ready", emit_on_first_run=True)
    runner = OuterLoopRunner(
        provider, config, loops_root=loops_root, inner_loop_launcher=launcher
    )
    runner.run_once()

    force_runner = OuterLoopRunner(
        provider,
        replace(config, force=True),
        loops_root=loops_root,
        inner_loop_launcher=launcher,
    )
    force_runner.run_once()

    run_dirs = list_run_dirs(loops_root)
    assert len(run_dirs) == 4
    assert len(launched) == 4


def test_emit_on_first_run_skips_launch(tmp_path: Path) -> None:
    tasks = [make_task("1", "Ship it"), make_task("2", "Next")]
    provider = StubProvider(tasks)
    loops_root = tmp_path / ".loops"
    launched: list[Path] = []

    def launcher(run_dir: Path, _task: Task) -> None:
        launched.append(run_dir)

    config = OuterLoopConfig(task_ready_status="Ready", emit_on_first_run=False)
    runner = OuterLoopRunner(
        provider, config, loops_root=loops_root, inner_loop_launcher=launcher
    )

    runner.run_once()
    assert list_run_dirs(loops_root) == []
    state = read_outer_state(loops_root / "outer_state.json")
    assert state.initialized is True
    assert len(state.tasks) == 2

    runner.run_once()
    assert list_run_dirs(loops_root) == []
    assert launched == []


def test_load_config_resolves_working_dir(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    payload = {
        "provider_id": "github_projects_v2",
        "provider_config": {},
        "inner_loop": {
            "command": "echo hello",
            "working_dir": "inner",
            "append_task_url": False,
        },
    }
    config_path.write_text(json.dumps(payload))

    config = load_config(config_path)
    assert config.inner_loop is not None
    assert config.inner_loop.append_task_url is False
    assert config.inner_loop.working_dir == str((tmp_path / "inner").resolve())


def test_load_config_rejects_bool_ints(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    payload = {
        "provider_id": "github_projects_v2",
        "provider_config": {},
        "loop_config": {"poll_interval_seconds": True},
    }
    config_path.write_text(json.dumps(payload))

    with pytest.raises(TypeError, match="poll_interval_seconds"):
        load_config(config_path)
