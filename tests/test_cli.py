from __future__ import annotations

import json
import sys
from pathlib import Path

from click.testing import CliRunner
import loops.cli as cli_module

from loops.__main__ import _normalize_argv
from loops.cli import main
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
