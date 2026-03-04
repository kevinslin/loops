from __future__ import annotations

from dataclasses import replace
import json
import subprocess
import sys
from pathlib import Path

import pytest

from loops.state.approval_config import (
    DEFAULT_APPROVAL_COMMENT_PATTERN,
)
from loops.core.handoff_handlers import HANDOFF_HANDLER_GH_COMMENT
from loops.state.inner_loop_runtime_config import (
    INNER_LOOP_RUNTIME_CONFIG_FILE,
    read_inner_loop_runtime_config,
)
from loops.core.outer_loop import (
    InnerLoopCommandConfig,
    LATEST_LOOPS_CONFIG_VERSION,
    LoopsConfig,
    OuterLoopConfig,
    OuterLoopRunner,
    SyncModeInterruptedError,
    build_provider,
    build_inner_loop_launcher,
    _is_loops_inner_loop_command,
    load_config,
    read_outer_state,
)
from loops.task_providers.github_projects_v2 import GithubProjectsV2TaskProvider
from loops.state.run_record import Task, read_run_record


class StubProvider:
    def __init__(self, tasks: list[Task]) -> None:
        self._tasks = list(tasks)

    def poll(self, limit: int | None = None) -> list[Task]:
        if limit is None:
            return list(self._tasks)
        return list(self._tasks)[:limit]


class RecordingProvider(StubProvider):
    def __init__(self, tasks: list[Task]) -> None:
        super().__init__(tasks)
        self.poll_limits: list[int | None] = []

    def poll(self, limit: int | None = None) -> list[Task]:
        self.poll_limits.append(limit)
        return super().poll(limit)


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
    runs_root = loops_root / "jobs"
    if not runs_root.exists():
        return []
    return sorted([path for path in runs_root.iterdir() if path.is_dir()])


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (["loops", "inner-loop"], True),
        (["uv", "run", "loops", "inner-loop"], True),
        ([sys.executable, "-m", "loops", "inner-loop"], True),
        (["uv", "run", sys.executable, "-X", "dev", "-m", "loops", "inner-loop"], True),
        (["python", "-m", "loops.other"], False),
        (["echo", "hello"], False),
    ],
)
def test_is_loops_inner_loop_command_detects_wrapped_invocations(
    command: list[str],
    expected: bool,
) -> None:
    assert _is_loops_inner_loop_command(command) is expected


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
    assert all(
        read_run_record(run_dir / "run.json").stream_logs_stdout is False
        for run_dir in run_dirs
    )
    assert all(
        read_run_record(run_dir / "run.json").checkout_mode == "branch"
        for run_dir in run_dirs
    )
    assert all(
        read_run_record(run_dir / "run.json").starting_commit == "unknown"
        for run_dir in run_dirs
    )
    assert all((run_dir / "agent.log").exists() for run_dir in run_dirs)
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


def test_run_once_forced_task_url_selects_task_and_ignores_ready_filter(
    tmp_path: Path,
) -> None:
    tasks = [
        make_task("1", "Ship it", status="Backlog"),
        make_task("2", "Next", status="In Progress"),
    ]
    provider = RecordingProvider(tasks)
    loops_root = tmp_path / ".loops"
    launched: list[Path] = []

    def launcher(run_dir: Path, _task: Task) -> None:
        launched.append(run_dir)

    config = OuterLoopConfig(
        task_ready_status="Ready",
        emit_on_first_run=False,
        force=True,
    )
    runner = OuterLoopRunner(
        provider, config, loops_root=loops_root, inner_loop_launcher=launcher
    )

    runner.run_once(limit=1, forced_task_url="https://example.com/2/?utm=1#frag")

    run_dirs = list_run_dirs(loops_root)
    assert len(run_dirs) == 1
    run_record = read_run_record(run_dirs[0] / "run.json")
    assert run_record.task.id == "2"
    assert len(launched) == 1
    assert provider.poll_limits == [None]
    state = read_outer_state(loops_root / "outer_state.json")
    assert list(state.tasks.keys()) == ["stub:2"]


def test_run_once_forced_task_url_raises_when_missing(tmp_path: Path) -> None:
    tasks = [make_task("1", "Ship it"), make_task("2", "Next")]
    provider = StubProvider(tasks)
    loops_root = tmp_path / ".loops"
    config = OuterLoopConfig(
        task_ready_status="Ready",
        emit_on_first_run=False,
        force=True,
    )
    runner = OuterLoopRunner(provider, config, loops_root=loops_root)

    with pytest.raises(ValueError, match="not found"):
        runner.run_once(forced_task_url="https://example.com/missing")


def test_run_once_forced_task_url_raises_on_ambiguous_match(tmp_path: Path) -> None:
    tasks = [
        Task(
            provider_id="stub",
            id="1",
            title="First",
            status="Ready",
            url="https://example.com/shared",
            created_at="2026-02-05T00:00:00Z",
            updated_at="2026-02-05T00:00:00Z",
        ),
        Task(
            provider_id="stub",
            id="2",
            title="Second",
            status="Ready",
            url="https://example.com/shared",
            created_at="2026-02-05T00:00:00Z",
            updated_at="2026-02-05T00:00:00Z",
        ),
    ]
    provider = StubProvider(tasks)
    loops_root = tmp_path / ".loops"
    config = OuterLoopConfig(
        task_ready_status="Ready",
        emit_on_first_run=False,
        force=True,
    )
    runner = OuterLoopRunner(provider, config, loops_root=loops_root)

    with pytest.raises(ValueError, match="matched multiple tasks"):
        runner.run_once(forced_task_url="https://example.com/shared")


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


def test_run_once_writes_detailed_logs(tmp_path: Path) -> None:
    task = make_task("1", "Ship it")
    provider = StubProvider([task])
    loops_root = tmp_path / ".loops"
    runner = OuterLoopRunner(
        provider,
        OuterLoopConfig(task_ready_status="Ready", emit_on_first_run=True),
        loops_root=loops_root,
        inner_loop_launcher=lambda _run_dir, _task: None,
    )

    run_dirs = runner.run_once(limit=3)

    log_text = (loops_root / "oloops.log").read_text()
    assert "run_once.start" in log_text
    assert "run_once.poll" in log_text
    assert "run_once.select" in log_text
    assert "run_once.schedule" in log_text
    assert "run_once.launch" in log_text
    assert "run_once.done" in log_text
    assert "ready=1 processed=1" in log_text
    assert f"run_dir={run_dirs[0]}" in log_text


def test_run_once_logs_and_persists_checkout_mode_and_starting_commit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    task = make_task("1", "Ship it")
    provider = StubProvider([task])
    loops_root = tmp_path / ".loops"
    runner = OuterLoopRunner(
        provider,
        OuterLoopConfig(
            task_ready_status="Ready",
            emit_on_first_run=True,
            checkout_mode="worktree",
        ),
        loops_root=loops_root,
        inner_loop_launcher=lambda _run_dir, _task: None,
    )
    monkeypatch.setattr(
        "loops.core.outer_loop._resolve_starting_commit",
        lambda _loops_root: "abc123",
    )

    run_dirs = runner.run_once(limit=1)

    run_record = read_run_record(run_dirs[0] / "run.json")
    assert run_record.checkout_mode == "worktree"
    assert run_record.starting_commit == "abc123"

    log_text = (loops_root / "oloops.log").read_text()
    assert "checkout_mode=worktree" in log_text
    assert "starting_commit=abc123" in log_text


def test_load_config_resolves_working_dir(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    payload = {
        "task_provider_id": "github_projects_v2",
        "task_provider_config": {},
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


def test_load_config_reads_sync_mode(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    payload = {
        "task_provider_id": "github_projects_v2",
        "task_provider_config": {},
        "loop_config": {"sync_mode": True},
        "inner_loop": {
            "command": ["echo", "hello"],
            "append_task_url": False,
        },
    }
    config_path.write_text(json.dumps(payload))

    config = load_config(config_path)
    assert config.loop_config.sync_mode is True
    assert config.version == LATEST_LOOPS_CONFIG_VERSION


def test_load_config_reads_auto_approve_enabled(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    payload = {
        "task_provider_id": "github_projects_v2",
        "task_provider_config": {},
        "loop_config": {"auto_approve_enabled": True},
        "inner_loop": {
            "command": ["echo", "hello"],
            "append_task_url": False,
        },
    }
    config_path.write_text(json.dumps(payload))

    config = load_config(config_path)
    assert config.loop_config.auto_approve_enabled is True
    assert config.version == LATEST_LOOPS_CONFIG_VERSION


def test_load_config_reads_handoff_handler(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    payload = {
        "task_provider_id": "github_projects_v2",
        "task_provider_config": {},
        "loop_config": {"handoff_handler": HANDOFF_HANDLER_GH_COMMENT},
    }
    config_path.write_text(json.dumps(payload))

    config = load_config(config_path)
    assert config.loop_config.handoff_handler == HANDOFF_HANDLER_GH_COMMENT


def test_load_config_reads_checkout_mode(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    payload = {
        "task_provider_id": "github_projects_v2",
        "task_provider_config": {},
        "loop_config": {"checkout_mode": "worktree"},
    }
    config_path.write_text(json.dumps(payload))

    config = load_config(config_path)
    assert config.loop_config.checkout_mode == "worktree"


def test_load_config_rejects_invalid_checkout_mode(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    payload = {
        "task_provider_id": "github_projects_v2",
        "task_provider_config": {},
        "loop_config": {"checkout_mode": "detached"},
    }
    config_path.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="checkout_mode"):
        load_config(config_path)


def test_load_config_migrates_legacy_provider_keys(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    payload = {
        "version": 1,
        "provider_id": "github_projects_v2",
        "provider_config": {"url": "https://github.com/orgs/acme/projects/1"},
    }
    config_path.write_text(json.dumps(payload))

    config = load_config(config_path)
    assert config.version == LATEST_LOOPS_CONFIG_VERSION
    assert config.task_provider_id == "github_projects_v2"
    assert config.task_provider_config == {
        "approval_comment_pattern": r"^\s*/approve\b",
        "approval_comment_usernames": [],
        "allowlist": [],
        "page_size": 50,
        "status_field": "Status",
        "url": "https://github.com/orgs/acme/projects/1",
    }


def test_load_config_migrates_versionless_legacy_provider_keys(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    payload = {
        "provider_id": "github_projects_v2",
        "provider_config": {"url": "https://github.com/orgs/acme/projects/2"},
    }
    config_path.write_text(json.dumps(payload))

    config = load_config(config_path)
    assert config.version == LATEST_LOOPS_CONFIG_VERSION
    assert config.task_provider_id == "github_projects_v2"
    assert config.task_provider_config == {
        "approval_comment_pattern": r"^\s*/approve\b",
        "approval_comment_usernames": [],
        "allowlist": [],
        "page_size": 50,
        "status_field": "Status",
        "url": "https://github.com/orgs/acme/projects/2",
    }


def test_load_config_rejects_invalid_handoff_handler(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    payload = {
        "task_provider_id": "github_projects_v2",
        "task_provider_config": {},
        "loop_config": {"handoff_handler": "unsupported_handler"},
    }
    config_path.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="handoff_handler"):
        load_config(config_path)


def test_load_config_rejects_gh_handoff_for_non_github_provider(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    payload = {
        "task_provider_id": "custom_provider",
        "task_provider_config": {},
        "loop_config": {"handoff_handler": HANDOFF_HANDLER_GH_COMMENT},
    }
    config_path.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="requires provider_id='github_projects_v2'"):
        load_config(config_path)


def test_load_config_reads_comment_approval_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    payload = {
        "task_provider_id": "github_projects_v2",
        "task_provider_config": {
            "approval_comment_usernames": ["Maintainer", "review-bot", "maintainer"],
            "approval_comment_pattern": r"^\s*/shipit\b",
        },
    }
    config_path.write_text(json.dumps(payload))

    config = load_config(config_path)
    assert config.version == LATEST_LOOPS_CONFIG_VERSION
    assert config.task_provider_config["approval_comment_usernames"] == [
        "Maintainer",
        "review-bot",
        "maintainer",
    ]
    assert config.task_provider_config["approval_comment_pattern"] == r"^\s*/shipit\b"


def test_load_config_migrates_legacy_loop_approval_keys_to_provider_config(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.json"
    payload = {
        "version": 2,
        "task_provider_id": "github_projects_v2",
        "task_provider_config": {
            "url": "https://github.com/orgs/acme/projects/1",
        },
        "loop_config": {
            "approval_comment_usernames": ["Maintainer", "review-bot"],
            "approval_comment_pattern": r"^\s*/shipit\b",
        },
    }
    config_path.write_text(json.dumps(payload))

    config = load_config(config_path)
    assert config.version == LATEST_LOOPS_CONFIG_VERSION
    assert config.task_provider_config["approval_comment_usernames"] == [
        "Maintainer",
        "review-bot",
    ]
    assert config.task_provider_config["approval_comment_pattern"] == r"^\s*/shipit\b"


def test_load_config_backfills_github_provider_defaults(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    payload = {
        "task_provider_id": "github_projects_v2",
        "task_provider_config": {"url": "https://github.com/orgs/acme/projects/1"},
    }
    config_path.write_text(json.dumps(payload))

    config = load_config(config_path)
    assert config.task_provider_config == {
        "approval_comment_pattern": r"^\s*/approve\b",
        "approval_comment_usernames": [],
        "allowlist": [],
        "page_size": 50,
        "status_field": "Status",
        "url": "https://github.com/orgs/acme/projects/1",
    }


def test_load_config_does_not_backfill_missing_github_provider_url(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.json"
    payload = {
        "task_provider_id": "github_projects_v2",
        "task_provider_config": {},
    }
    config_path.write_text(json.dumps(payload))

    config = load_config(config_path)
    assert config.task_provider_config == {
        "approval_comment_pattern": r"^\s*/approve\b",
        "approval_comment_usernames": [],
        "allowlist": [],
        "page_size": 50,
        "status_field": "Status",
    }

    monkeypatch.setenv("GITHUB_TOKEN", "token")
    with pytest.raises(ValueError, match="task_provider_config is invalid"):
        build_provider(config)


def test_load_config_rejects_invalid_comment_approval_usernames(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.json"
    payload = {
        "task_provider_id": "github_projects_v2",
        "task_provider_config": {
            "url": "https://github.com/orgs/acme/projects/1",
            "approval_comment_usernames": "maintainer",
        },
    }
    config_path.write_text(json.dumps(payload))

    config = load_config(config_path)
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    with pytest.raises(ValueError, match="task_provider_config is invalid"):
        build_provider(config)


def test_load_config_rejects_bool_ints(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    payload = {
        "task_provider_id": "github_projects_v2",
        "task_provider_config": {},
        "loop_config": {"poll_interval_seconds": True},
    }
    config_path.write_text(json.dumps(payload))

    with pytest.raises(TypeError, match="poll_interval_seconds"):
        load_config(config_path)


def test_build_provider_accepts_alias_secret_env_var(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "token-from-alias")
    config = LoopsConfig(
        version=LATEST_LOOPS_CONFIG_VERSION,
        task_provider_id="github_projects_v2",
        task_provider_config={"url": "https://github.com/orgs/acme/projects/1"},
        loop_config=OuterLoopConfig(),
        inner_loop=None,
    )

    provider = build_provider(config)

    assert isinstance(provider, GithubProjectsV2TaskProvider)


def test_build_provider_uses_explicit_environ_for_required_secrets(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    config = LoopsConfig(
        version=LATEST_LOOPS_CONFIG_VERSION,
        task_provider_id="github_projects_v2",
        task_provider_config={"url": "https://github.com/orgs/acme/projects/1"},
        loop_config=OuterLoopConfig(),
        inner_loop=None,
    )

    provider = build_provider(config, environ={"GITHUB_TOKEN": "token-from-runtime"})

    assert isinstance(provider, GithubProjectsV2TaskProvider)


def test_build_provider_rejects_missing_required_secret(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    config = LoopsConfig(
        version=LATEST_LOOPS_CONFIG_VERSION,
        task_provider_id="github_projects_v2",
        task_provider_config={"url": "https://github.com/orgs/acme/projects/1"},
        loop_config=OuterLoopConfig(),
        inner_loop=None,
    )

    with pytest.raises(ValueError, match=r"GITHUB_TOKEN, GH_TOKEN"):
        build_provider(config)


def test_build_provider_validates_provider_config(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    config = LoopsConfig(
        version=LATEST_LOOPS_CONFIG_VERSION,
        task_provider_id="github_projects_v2",
        task_provider_config={
            "url": "https://github.com/orgs/acme/projects/1",
            "unsupported": "value",
        },
        loop_config=OuterLoopConfig(),
        inner_loop=None,
    )

    with pytest.raises(ValueError, match="task_provider_config is invalid"):
        build_provider(config)


def test_run_once_persists_state_on_launch_error(tmp_path: Path) -> None:
    tasks = [make_task("1", "Ship it")]
    provider = StubProvider(tasks)
    loops_root = tmp_path / ".loops"

    def launcher(_run_dir: Path, _task: Task) -> None:
        raise RuntimeError("boom")

    config = OuterLoopConfig(task_ready_status="Ready", emit_on_first_run=True)
    runner = OuterLoopRunner(
        provider, config, loops_root=loops_root, inner_loop_launcher=launcher
    )

    with pytest.raises(RuntimeError, match="boom"):
        runner.run_once()

    state = read_outer_state(loops_root / "outer_state.json")
    assert state.initialized is True
    assert len(state.tasks) == 1


def test_build_inner_loop_launcher_sync_mode_uses_subprocess_run(
    tmp_path: Path, monkeypatch
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    task = make_task("1", "Ship it")

    config = LoopsConfig(
        version=LATEST_LOOPS_CONFIG_VERSION,
        task_provider_id="github_projects_v2",
        task_provider_config={"url": "https://github.com/orgs/acme/projects/1"},
        loop_config=OuterLoopConfig(sync_mode=True),
        inner_loop=InnerLoopCommandConfig(
            command=["echo", "hello"],
            append_task_url=False,
        ),
    )
    launcher = build_inner_loop_launcher(
        config,
        approval_comment_usernames=("maintainer", "review-bot"),
        approval_comment_pattern=r"^\s*/shipit\b",
    )

    captured: dict[str, object] = {}

    def fake_run(command, *, cwd, env, check):
        captured["command"] = command
        captured["cwd"] = cwd
        captured["env"] = env
        captured["check"] = check
        return subprocess.CompletedProcess(command, 0)

    def fail_popen(*_args, **_kwargs):
        raise AssertionError("subprocess.Popen should not be used in sync_mode")

    monkeypatch.setattr("loops.core.outer_loop.subprocess.run", fake_run)
    monkeypatch.setattr("loops.core.outer_loop.subprocess.Popen", fail_popen)
    monkeypatch.delenv("LOOPS_TASK_ID", raising=False)
    monkeypatch.delenv("LOOPS_TASK_TITLE", raising=False)
    monkeypatch.delenv("LOOPS_TASK_URL", raising=False)
    monkeypatch.delenv("LOOPS_TASK_PROVIDER", raising=False)
    monkeypatch.delenv("LOOPS_HANDOFF_HANDLER", raising=False)
    monkeypatch.delenv("LOOPS_AUTO_APPROVE_ENABLED", raising=False)
    monkeypatch.delenv("LOOPS_STREAM_LOGS_STDOUT", raising=False)

    launcher(run_dir, task)

    assert captured["command"] == ["echo", "hello"]
    assert captured["check"] is False
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["LOOPS_RUN_DIR"] == str(run_dir)
    assert "LOOPS_TASK_ID" not in env
    assert "LOOPS_HANDOFF_HANDLER" not in env
    assert "LOOPS_AUTO_APPROVE_ENABLED" not in env
    assert "LOOPS_STREAM_LOGS_STDOUT" not in env
    runtime_config = read_inner_loop_runtime_config(run_dir)
    assert runtime_config is not None
    assert runtime_config.handoff_handler == "stdin_handler"
    assert runtime_config.auto_approve_enabled is False
    assert runtime_config.stream_logs_stdout is True
    assert runtime_config.env is None
    runtime_config_path = run_dir / INNER_LOOP_RUNTIME_CONFIG_FILE
    assert runtime_config_path.exists()
    assert runtime_config_path.stat().st_mode & 0o777 == 0o600


def test_build_inner_loop_launcher_sync_mode_interrupt_raises_typed_error(
    tmp_path: Path, monkeypatch
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    task = make_task("1", "Ship it")

    config = LoopsConfig(
        version=LATEST_LOOPS_CONFIG_VERSION,
        task_provider_id="github_projects_v2",
        task_provider_config={"url": "https://github.com/orgs/acme/projects/1"},
        loop_config=OuterLoopConfig(sync_mode=True),
        inner_loop=InnerLoopCommandConfig(
            command=["echo", "hello"],
            append_task_url=False,
        ),
    )
    launcher = build_inner_loop_launcher(config)

    def fake_run(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("loops.core.outer_loop.subprocess.run", fake_run)

    with pytest.raises(SyncModeInterruptedError) as exc_info:
        launcher(run_dir, task)

    assert exc_info.value.run_dir == run_dir


def test_build_inner_loop_launcher_writes_runtime_env_to_run_config(
    tmp_path: Path, monkeypatch
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    task = make_task("1", "Ship it")
    config = LoopsConfig(
        version=LATEST_LOOPS_CONFIG_VERSION,
        task_provider_id="github_projects_v2",
        task_provider_config={"url": "https://github.com/orgs/acme/projects/1"},
        loop_config=OuterLoopConfig(sync_mode=False),
        inner_loop=InnerLoopCommandConfig(
            command=["echo", "hello"],
            env={"CODEX_CMD": "codex exec --json", "CUSTOM_VAR": "present"},
            append_task_url=False,
        ),
    )
    launcher = build_inner_loop_launcher(config)
    captured: dict[str, object] = {}

    def fake_popen(command, *, cwd, stdout, stderr, env):
        captured["command"] = command
        captured["cwd"] = cwd
        captured["stdout"] = stdout
        captured["stderr"] = stderr
        captured["env"] = env
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("loops.core.outer_loop.subprocess.Popen", fake_popen)
    monkeypatch.delenv("CODEX_CMD", raising=False)
    monkeypatch.delenv("CUSTOM_VAR", raising=False)

    launcher(run_dir, task)

    runtime_config = read_inner_loop_runtime_config(run_dir)
    assert runtime_config is not None
    assert runtime_config.env == {
        "CODEX_CMD": "codex exec --json",
        "CUSTOM_VAR": "present",
    }
    assert (run_dir / INNER_LOOP_RUNTIME_CONFIG_FILE).stat().st_mode & 0o777 == 0o600
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["LOOPS_RUN_DIR"] == str(run_dir)
    assert env["CODEX_CMD"] == "codex exec --json"
    assert env["CUSTOM_VAR"] == "present"


def test_build_inner_loop_launcher_does_not_inject_runtime_env_for_loops_inner_loop_command(
    tmp_path: Path, monkeypatch
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    task = make_task("1", "Ship it")
    config = LoopsConfig(
        version=LATEST_LOOPS_CONFIG_VERSION,
        task_provider_id="github_projects_v2",
        task_provider_config={"url": "https://github.com/orgs/acme/projects/1"},
        loop_config=OuterLoopConfig(sync_mode=False),
        inner_loop=InnerLoopCommandConfig(
            command=[sys.executable, "-m", "loops", "inner-loop"],
            env={"CODEX_CMD": "codex exec --json", "CUSTOM_VAR": "present"},
            append_task_url=False,
        ),
    )
    launcher = build_inner_loop_launcher(config)
    captured: dict[str, object] = {}

    def fake_popen(command, *, cwd, stdout, stderr, env):
        captured["command"] = command
        captured["cwd"] = cwd
        captured["stdout"] = stdout
        captured["stderr"] = stderr
        captured["env"] = env
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("loops.core.outer_loop.subprocess.Popen", fake_popen)
    monkeypatch.delenv("CODEX_CMD", raising=False)
    monkeypatch.delenv("CUSTOM_VAR", raising=False)

    launcher(run_dir, task)

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["LOOPS_RUN_DIR"] == str(run_dir)
    assert "CODEX_CMD" not in env
    assert "CUSTOM_VAR" not in env


def test_run_once_sync_mode_streams_outer_logs_to_stdout(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider = StubProvider([make_task("1", "Ship it")])
    loops_root = tmp_path / ".loops"
    runner = OuterLoopRunner(
        provider,
        OuterLoopConfig(
            task_ready_status="Ready",
            emit_on_first_run=True,
            sync_mode=True,
        ),
        loops_root=loops_root,
        inner_loop_launcher=lambda _run_dir, _task: None,
    )

    runner.run_once()

    captured = capsys.readouterr()
    assert "run_once.start" in captured.out
    assert "run_once.launch" in captured.out
    assert "run_once.done" in captured.out
    run_dirs = list_run_dirs(loops_root)
    assert len(run_dirs) == 1
    assert read_run_record(run_dirs[0] / "run.json").stream_logs_stdout is True


def test_load_config_preserves_explicit_version(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    payload = {
        "version": LATEST_LOOPS_CONFIG_VERSION,
        "task_provider_id": "github_projects_v2",
        "task_provider_config": {},
    }
    config_path.write_text(json.dumps(payload))

    config = load_config(config_path)
    assert config.version == LATEST_LOOPS_CONFIG_VERSION


def test_build_inner_loop_launcher_writes_custom_comment_approval_settings(
    tmp_path: Path, monkeypatch
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    task = make_task("1", "Ship it")
    config = LoopsConfig(
        version=LATEST_LOOPS_CONFIG_VERSION,
        task_provider_id="github_projects_v2",
        task_provider_config={"url": "https://github.com/orgs/acme/projects/1"},
        loop_config=OuterLoopConfig(),
        inner_loop=InnerLoopCommandConfig(
            command=["echo", "hello"],
            append_task_url=False,
        ),
    )
    launcher = build_inner_loop_launcher(
        config,
        approval_comment_usernames=("maintainer", "review-bot"),
        approval_comment_pattern=r"^\s*/shipit\b",
    )

    def fake_popen(command, *, cwd, stdout, stderr, env):
        del cwd, stdout, stderr, env
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("loops.core.outer_loop.subprocess.Popen", fake_popen)
    launcher(run_dir, task)

    runtime_config = read_inner_loop_runtime_config(run_dir)
    assert runtime_config is not None
    assert runtime_config.approval_comment_usernames == ("maintainer", "review-bot")
    assert runtime_config.approval_comment_pattern == r"^\s*/shipit\b"
    assert runtime_config.review_actor_usernames == ()


def test_build_inner_loop_launcher_writes_default_comment_approval_settings(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    task = make_task("1", "Ship it")
    config = LoopsConfig(
        version=LATEST_LOOPS_CONFIG_VERSION,
        task_provider_id="github_projects_v2",
        task_provider_config={"url": "https://github.com/orgs/acme/projects/1"},
        loop_config=OuterLoopConfig(),
        inner_loop=InnerLoopCommandConfig(
            command=["echo", "hello"],
            append_task_url=False,
        ),
    )
    launcher = build_inner_loop_launcher(config)

    def fake_popen(command, *, cwd, stdout, stderr, env):
        del cwd, stdout, stderr, env
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("loops.core.outer_loop.subprocess.Popen", fake_popen)
    launcher(run_dir, task)

    runtime_config = read_inner_loop_runtime_config(run_dir)
    assert runtime_config is not None
    assert runtime_config.approval_comment_usernames == ()
    assert runtime_config.approval_comment_pattern == DEFAULT_APPROVAL_COMMENT_PATTERN
    assert runtime_config.review_actor_usernames == ()


def test_build_inner_loop_launcher_writes_review_actor_allowlist_to_runtime_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    task = make_task("1", "Ship it")
    config = LoopsConfig(
        version=LATEST_LOOPS_CONFIG_VERSION,
        task_provider_id="github_projects_v2",
        task_provider_config={"url": "https://github.com/orgs/acme/projects/1"},
        loop_config=OuterLoopConfig(),
        inner_loop=InnerLoopCommandConfig(
            command=["echo", "hello"],
            append_task_url=False,
        ),
    )
    launcher = build_inner_loop_launcher(
        config,
        review_actor_usernames=("Maintainer", "review-bot", "maintainer"),
    )

    def fake_popen(command, *, cwd, stdout, stderr, env):
        del cwd, stdout, stderr, env
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("loops.core.outer_loop.subprocess.Popen", fake_popen)
    launcher(run_dir, task)

    runtime_config = read_inner_loop_runtime_config(run_dir)
    assert runtime_config is not None
    assert runtime_config.review_actor_usernames == ("maintainer", "review-bot")
