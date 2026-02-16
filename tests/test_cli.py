from __future__ import annotations

import json
import sys
from pathlib import Path

from click.testing import CliRunner
import loops.cli as cli_module

from loops.__main__ import _normalize_argv
from loops.cli import main
from loops.outer_loop import LoopsConfig, OuterLoopConfig
from loops.run_record import RunPR, RunRecord, Task, read_run_record, write_run_record


def test_init_creates_default_loops_structure(tmp_path: Path) -> None:
    runner = CliRunner()
    loops_root = tmp_path / ".loops"

    result = runner.invoke(main, ["init", "--loops-root", str(loops_root)])

    assert result.exit_code == 0, result.output
    assert (loops_root / "config.json").exists()
    assert (loops_root / "outer_state.json").exists()
    assert (loops_root / "oloops.log").exists()
    assert (loops_root / "jobs").exists()

    config_payload = json.loads((loops_root / "config.json").read_text())
    assert config_payload["provider_id"] == "github_projects_v2"
    assert config_payload["loop_config"]["sync_mode"] is False
    assert config_payload["inner_loop"]["append_task_url"] is False
    assert config_payload["inner_loop"]["command"] == [
        sys.executable,
        "-m",
        "loops.inner_loop",
    ]


def test_init_rejects_existing_config_without_force(tmp_path: Path) -> None:
    runner = CliRunner()
    loops_root = tmp_path / ".loops"
    loops_root.mkdir(parents=True)
    config_path = loops_root / "config.json"
    config_path.write_text('{"marker": true}')

    result = runner.invoke(main, ["init", "--loops-root", str(loops_root)])

    assert result.exit_code != 0
    assert "Config already exists" in result.output
    assert json.loads(config_path.read_text()) == {"marker": True}


def test_init_force_overwrites_existing_config(tmp_path: Path) -> None:
    runner = CliRunner()
    loops_root = tmp_path / ".loops"
    loops_root.mkdir(parents=True)
    config_path = loops_root / "config.json"
    config_path.write_text('{"marker": true}')

    result = runner.invoke(main, ["init", "--loops-root", str(loops_root), "--force"])

    assert result.exit_code == 0, result.output
    overwritten = json.loads(config_path.read_text())
    assert overwritten["provider_id"] == "github_projects_v2"


def test_normalize_argv_preserves_known_subcommands() -> None:
    argv = ["python", "init"]
    assert _normalize_argv(argv) == argv


def test_normalize_argv_routes_legacy_flags_to_run() -> None:
    argv = ["python", "--run-once"]
    assert _normalize_argv(argv) == ["python", "run", "--run-once"]


def test_normalize_argv_defaults_to_run_when_no_args() -> None:
    argv = ["python"]
    assert _normalize_argv(argv) == ["python", "run"]


def test_inner_loop_reset_creates_initial_run_record_when_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = CliRunner()
    run_dir = tmp_path / "run"
    env = {
        "LOOPS_TASK_PROVIDER": "github_projects_v2",
        "LOOPS_TASK_ID": "123",
        "LOOPS_TASK_TITLE": "Reset task",
        "LOOPS_TASK_URL": "https://github.com/acme/api/issues/123",
    }

    def _should_not_run(*_args, **_kwargs) -> None:
        raise AssertionError("should not run")

    monkeypatch.setattr(cli_module, "run_inner_loop", _should_not_run)

    result = runner.invoke(
        main,
        ["inner-loop", "--run-dir", str(run_dir), "--reset"],
        env=env,
    )

    assert result.exit_code == 0, result.output
    record = read_run_record(run_dir / "run.json")
    assert record.task.provider_id == "github_projects_v2"
    assert record.task.id == "123"
    assert record.task.title == "Reset task"
    assert record.task.url == "https://github.com/acme/api/issues/123"
    assert record.pr is None
    assert record.codex_session is None
    assert record.needs_user_input is False
    assert record.last_state == "RUNNING"


def test_inner_loop_reset_preserves_existing_task(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    write_run_record(
        run_dir / "run.json",
        RunRecord(
            task=Task(
                provider_id="github",
                id="4",
                title="Existing task",
                status="ready",
                url="https://github.com/acme/api/issues/4",
                created_at="2026-02-09T00:00:00Z",
                updated_at="2026-02-09T00:00:00Z",
            ),
            pr=None,
            codex_session=None,
            needs_user_input=True,
            needs_user_input_payload={"message": "old prompt"},
            last_state="NEEDS_INPUT",
            updated_at="",
        ),
    )

    def _should_not_run(*_args, **_kwargs) -> None:
        raise AssertionError("should not run")

    monkeypatch.setattr(cli_module, "run_inner_loop", _should_not_run)

    result = runner.invoke(main, ["inner-loop", "--run-dir", str(run_dir), "--reset"])

    assert result.exit_code == 0, result.output
    record = read_run_record(run_dir / "run.json")
    assert record.task.id == "4"
    assert record.task.title == "Existing task"
    assert record.task.url == "https://github.com/acme/api/issues/4"
    assert record.pr is None
    assert record.codex_session is None
    assert record.needs_user_input is False
    assert record.needs_user_input_payload is None
    assert record.last_state == "RUNNING"


def test_inner_loop_reset_preserves_existing_pr_link(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    write_run_record(
        run_dir / "run.json",
        RunRecord(
            task=Task(
                provider_id="github",
                id="9",
                title="Existing pr task",
                status="ready",
                url="https://github.com/acme/api/issues/9",
                created_at="2026-02-09T00:00:00Z",
                updated_at="2026-02-09T00:00:00Z",
            ),
            pr=RunPR(
                url="https://github.com/acme/api/pull/42",
                number=42,
                repo="acme/api",
                review_status="approved",
                merged_at="2026-02-09T00:03:00Z",
                last_checked_at="2026-02-09T00:03:00Z",
                latest_review_submitted_at="2026-02-09T00:02:00Z",
                review_addressed_at="2026-02-09T00:01:00Z",
            ),
            codex_session=None,
            needs_user_input=True,
            needs_user_input_payload={"message": "old prompt"},
            last_state="NEEDS_INPUT",
            updated_at="",
        ),
    )

    def _should_not_run(*_args, **_kwargs) -> None:
        raise AssertionError("should not run")

    monkeypatch.setattr(cli_module, "run_inner_loop", _should_not_run)

    result = runner.invoke(main, ["inner-loop", "--run-dir", str(run_dir), "--reset"])

    assert result.exit_code == 0, result.output
    record = read_run_record(run_dir / "run.json")
    assert record.pr is not None
    assert record.pr.url == "https://github.com/acme/api/pull/42"
    assert record.pr.number == 42
    assert record.pr.repo == "acme/api"
    assert record.pr.review_status == "open"
    assert record.pr.merged_at is None
    assert record.pr.last_checked_at is None
    assert record.pr.latest_review_submitted_at is None
    assert record.pr.review_addressed_at is None
    assert record.last_state == "WAITING_ON_REVIEW"


def test_run_command_passes_task_url_to_outer_loop(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    config_path = tmp_path / "config.json"
    config_path.write_text("{}")
    captured: dict[str, object] = {}

    def fake_run_outer_loop(
        *,
        config_path: Path,
        run_once: bool,
        limit: int | None,
        force: bool | None,
        task_url: str | None,
    ) -> None:
        captured["config_path"] = config_path
        captured["run_once"] = run_once
        captured["limit"] = limit
        captured["force"] = force
        captured["task_url"] = task_url

    monkeypatch.setattr(cli_module, "_run_outer_loop", fake_run_outer_loop)

    result = runner.invoke(
        main,
        [
            "run",
            "--config",
            str(config_path),
            "--run-once",
            "--limit",
            "3",
            "--task-url",
            "https://github.com/acme/api/issues/42",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["config_path"] == config_path
    assert captured["run_once"] is True
    assert captured["limit"] == 3
    assert captured["force"] is None
    assert captured["task_url"] == "https://github.com/acme/api/issues/42"


def test_run_outer_loop_task_url_implies_run_once_and_force(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text("{}")
    loaded = LoopsConfig(
        provider_id="github_projects_v2",
        provider_config={
            "url": "https://github.com/orgs/default/projects/1",
            "status_field": "Status",
        },
        loop_config=OuterLoopConfig(force=False),
        inner_loop=None,
    )
    captured: dict[str, object] = {}
    provider = object()
    launcher = object()

    class FakeRunner:
        def __init__(
            self,
            provider_arg: object,
            config_arg: OuterLoopConfig,
            *,
            loops_root: Path,
            inner_loop_launcher: object,
        ) -> None:
            captured["provider_arg"] = provider_arg
            captured["config_arg"] = config_arg
            captured["loops_root"] = loops_root
            captured["inner_loop_launcher"] = inner_loop_launcher

        def run_once(
            self,
            *,
            limit: int | None = None,
            forced_task_url: str | None = None,
        ) -> None:
            captured["run_once_limit"] = limit
            captured["run_once_task_url"] = forced_task_url

        def run_forever(self, *, limit: int | None = None) -> None:
            captured["run_forever_limit"] = limit

    def fake_build_provider(config: LoopsConfig) -> object:
        captured["provider_config"] = dict(config.provider_config)
        captured["inner_loop_command"] = (
            list(config.inner_loop.command) if config.inner_loop is not None else None
        )
        return provider

    monkeypatch.setattr(cli_module, "load_config", lambda _path: loaded)
    monkeypatch.setattr(cli_module, "build_provider", fake_build_provider)
    monkeypatch.setattr(cli_module, "build_inner_loop_launcher", lambda _config: launcher)
    monkeypatch.setattr(cli_module, "OuterLoopRunner", FakeRunner)

    cli_module._run_outer_loop(
        config_path=config_path,
        run_once=False,
        limit=7,
        force=False,
        task_url="https://github.com/acme/api/issues/9",
    )

    assert captured["provider_config"] == {
        "url": "https://github.com/orgs/default/projects/1",
        "status_field": "Status",
    }
    assert captured["inner_loop_command"] == [sys.executable, "-m", "loops.inner_loop"]
    loop_config = captured["config_arg"]
    assert isinstance(loop_config, OuterLoopConfig)
    assert loop_config.force is True
    assert captured["provider_arg"] is provider
    assert captured["inner_loop_launcher"] is launcher
    assert captured["run_once_limit"] == 7
    assert captured["run_once_task_url"] == "https://github.com/acme/api/issues/9"
    assert "run_forever_limit" not in captured
