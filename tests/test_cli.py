from __future__ import annotations

from dataclasses import asdict
import json
import subprocess
import sys
from pathlib import Path

import click
from click.testing import CliRunner
import pytest
import loops.core.cli as cli_module

from loops.__main__ import _normalize_argv, entrypoint
from loops.core.cli import main
from loops.state.inner_loop_runtime_config import (
    InnerLoopRuntimeConfig,
    write_inner_loop_runtime_config,
)
from loops.state.constants import STATE_HOOKS_LEDGER_FILE
from loops.core.outer_loop import (
    LATEST_LOOPS_CONFIG_VERSION,
    LoopsConfig,
    OuterLoopConfig,
    SyncModeInterruptedError,
    build_default_loop_config_payload,
    load_config,
)
from loops.task_providers.github_projects_v2 import build_default_provider_config_payload
from loops.state.run_record import RunPR, RunRecord, Task, read_run_record, write_run_record


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
    assert config_payload["version"] == LATEST_LOOPS_CONFIG_VERSION
    assert config_payload["task_provider_id"] == "github_projects_v2"
    assert config_payload["task_provider_config"] == build_default_provider_config_payload()
    assert config_payload["loop_config"] == build_default_loop_config_payload()
    assert config_payload["inner_loop"] == cli_module._build_default_inner_loop_payload()
    assert config_payload["loop_config"]["sync_mode"] is False
    assert config_payload["task_provider_config"]["approval_comment_usernames"] == []
    assert (
        config_payload["task_provider_config"]["approval_comment_pattern"]
        == r"^\s*/approve\b"
    )
    assert config_payload["loop_config"]["auto_approve_enabled"] is False
    assert config_payload["loop_config"]["handoff_handler"] == "stdin_handler"
    assert config_payload["loop_config"]["checkout_mode"] == "branch"
    assert config_payload["inner_loop"]["append_task_url"] is False
    assert config_payload["inner_loop"]["command"] == [
        sys.executable,
        "-m",
        "loops",
        "inner-loop",
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
    assert overwritten["task_provider_id"] == "github_projects_v2"


def test_clean_dry_run_reports_actions_without_modifying_runs(tmp_path: Path) -> None:
    runner = CliRunner()
    loops_root = tmp_path / ".loops"
    jobs_root = loops_root / "jobs"
    jobs_root.mkdir(parents=True)

    empty_run = jobs_root / "empty-run"
    empty_run.mkdir()
    (empty_run / "run.log").write_text("")
    (empty_run / "agent.log").write_text("")

    done_run = jobs_root / "done-run"
    done_run.mkdir()
    (done_run / "run.log").write_text("has output")
    (done_run / "agent.log").write_text("has output")
    write_run_record(
        done_run / "run.json",
        RunRecord(
            task=Task(
                provider_id="github_projects_v2",
                id="done",
                title="Done task",
                status="Done",
                url="https://github.com/acme/api/issues/1",
                created_at="2026-03-01T00:00:00Z",
                updated_at="2026-03-01T00:00:00Z",
            ),
            pr=RunPR(
                url="https://github.com/acme/api/pull/1",
                merged_at="2026-03-01T00:00:01Z",
            ),
            codex_session=None,
            needs_user_input=False,
            last_state="RUNNING",
            updated_at="",
        ),
    )

    active_run = jobs_root / "active-run"
    active_run.mkdir()
    (active_run / "run.log").write_text("has output")
    (active_run / "agent.log").write_text("has output")
    write_run_record(
        active_run / "run.json",
        RunRecord(
            task=Task(
                provider_id="github_projects_v2",
                id="active",
                title="Active task",
                status="In progress",
                url="https://github.com/acme/api/issues/2",
                created_at="2026-03-01T00:00:00Z",
                updated_at="2026-03-01T00:00:00Z",
            ),
            pr=None,
            codex_session=None,
            needs_user_input=False,
            last_state="RUNNING",
            updated_at="",
        ),
    )

    result = runner.invoke(
        main,
        ["clean", "--loops-root", str(loops_root), "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert "Would delete 1 empty run dir(s)." in result.output
    assert "Would archive 1 completed run dir(s)." in result.output
    assert str(empty_run.resolve()) in result.output
    assert str(done_run.resolve()) in result.output
    assert empty_run.exists()
    assert done_run.exists()
    assert active_run.exists()
    assert not (loops_root / ".archive").exists()


def test_clean_applies_deletes_and_archives_runs(tmp_path: Path) -> None:
    runner = CliRunner()
    loops_root = tmp_path / ".loops"
    jobs_root = loops_root / "jobs"
    jobs_root.mkdir(parents=True)

    empty_run = jobs_root / "empty-run"
    empty_run.mkdir()
    (empty_run / "run.log").write_text("")
    (empty_run / "agent.log").write_text("")

    done_run = jobs_root / "done-run"
    done_run.mkdir()
    (done_run / "run.log").write_text("has output")
    (done_run / "agent.log").write_text("has output")
    write_run_record(
        done_run / "run.json",
        RunRecord(
            task=Task(
                provider_id="github_projects_v2",
                id="done",
                title="Done task",
                status="Done",
                url="https://github.com/acme/api/issues/1",
                created_at="2026-03-01T00:00:00Z",
                updated_at="2026-03-01T00:00:00Z",
            ),
            pr=RunPR(
                url="https://github.com/acme/api/pull/1",
                merged_at="2026-03-01T00:00:01Z",
            ),
            codex_session=None,
            needs_user_input=False,
            last_state="RUNNING",
            updated_at="",
        ),
    )

    active_run = jobs_root / "active-run"
    active_run.mkdir()
    (active_run / "run.log").write_text("has output")
    (active_run / "agent.log").write_text("has output")
    write_run_record(
        active_run / "run.json",
        RunRecord(
            task=Task(
                provider_id="github_projects_v2",
                id="active",
                title="Active task",
                status="In progress",
                url="https://github.com/acme/api/issues/2",
                created_at="2026-03-01T00:00:00Z",
                updated_at="2026-03-01T00:00:00Z",
            ),
            pr=None,
            codex_session=None,
            needs_user_input=False,
            last_state="RUNNING",
            updated_at="",
        ),
    )

    result = runner.invoke(main, ["clean", "--loops-root", str(loops_root)])

    assert result.exit_code == 0, result.output
    assert "Deleted 1 empty run dir(s)." in result.output
    assert "Archived 1 completed run dir(s)." in result.output
    assert not empty_run.exists()
    assert not done_run.exists()
    assert active_run.exists()
    archived_done = loops_root / ".archive" / "done-run"
    assert archived_done.exists()
    assert (archived_done / "run.json").exists()


def test_clean_does_not_delete_active_runs_with_empty_logs(tmp_path: Path) -> None:
    runner = CliRunner()
    loops_root = tmp_path / ".loops"
    jobs_root = loops_root / "jobs"
    jobs_root.mkdir(parents=True)

    active_empty_run = jobs_root / "active-empty-run"
    active_empty_run.mkdir()
    (active_empty_run / "run.log").write_text("")
    (active_empty_run / "agent.log").write_text("")
    write_run_record(
        active_empty_run / "run.json",
        RunRecord(
            task=Task(
                provider_id="github_projects_v2",
                id="active-empty",
                title="Active empty run",
                status="In progress",
                url="https://github.com/acme/api/issues/3",
                created_at="2026-03-01T00:00:00Z",
                updated_at="2026-03-01T00:00:00Z",
            ),
            pr=None,
            codex_session=None,
            needs_user_input=False,
            last_state="RUNNING",
            updated_at="",
        ),
    )

    result = runner.invoke(main, ["clean", "--loops-root", str(loops_root)])

    assert result.exit_code == 0, result.output
    assert "Deleted 0 empty run dir(s)." in result.output
    assert active_empty_run.exists()


def test_clean_archives_done_runs_even_when_logs_are_empty(tmp_path: Path) -> None:
    runner = CliRunner()
    loops_root = tmp_path / ".loops"
    jobs_root = loops_root / "jobs"
    jobs_root.mkdir(parents=True)

    done_empty_run = jobs_root / "done-empty-run"
    done_empty_run.mkdir()
    (done_empty_run / "run.log").write_text("")
    (done_empty_run / "agent.log").write_text("")
    (done_empty_run / "run.json").write_text(
        json.dumps(
            {
                "task": {
                    "provider_id": "github_projects_v2",
                    "id": "done-empty",
                    "title": "Done empty run",
                    "status": "Done",
                    "url": "https://github.com/acme/api/issues/4",
                    "created_at": "2026-03-01T00:00:00Z",
                    "updated_at": "2026-03-01T00:00:00Z",
                },
                "needs_user_input": False,
                "last_state": "DONE",
                "updated_at": "2026-03-01T00:00:01Z",
            }
        )
    )

    result = runner.invoke(main, ["clean", "--loops-root", str(loops_root)])

    assert result.exit_code == 0, result.output
    assert "Deleted 0 empty run dir(s)." in result.output
    assert "Archived 1 completed run dir(s)." in result.output
    assert not done_empty_run.exists()
    assert (loops_root / ".archive" / "done-empty-run").exists()


def test_clean_uses_suffix_when_archive_target_exists(tmp_path: Path) -> None:
    runner = CliRunner()
    loops_root = tmp_path / ".loops"
    jobs_root = loops_root / "jobs"
    jobs_root.mkdir(parents=True)
    archive_root = loops_root / ".archive"
    (archive_root / "done-run").mkdir(parents=True)

    done_run = jobs_root / "done-run"
    done_run.mkdir()
    (done_run / "run.log").write_text("has output")
    (done_run / "agent.log").write_text("has output")
    write_run_record(
        done_run / "run.json",
        RunRecord(
            task=Task(
                provider_id="github_projects_v2",
                id="done",
                title="Done task",
                status="Done",
                url="https://github.com/acme/api/issues/1",
                created_at="2026-03-01T00:00:00Z",
                updated_at="2026-03-01T00:00:00Z",
            ),
            pr=RunPR(
                url="https://github.com/acme/api/pull/1",
                merged_at="2026-03-01T00:00:01Z",
            ),
            codex_session=None,
            needs_user_input=False,
            last_state="RUNNING",
            updated_at="",
        ),
    )

    result = runner.invoke(main, ["clean", "--loops-root", str(loops_root)])

    assert result.exit_code == 0, result.output
    assert not done_run.exists()
    archived_done = archive_root / "done-run-1"
    assert archived_done.exists()
    assert str(archived_done.resolve()) in result.output


def test_clean_archives_done_runs_even_with_schema_drift(tmp_path: Path) -> None:
    runner = CliRunner()
    loops_root = tmp_path / ".loops"
    jobs_root = loops_root / "jobs"
    jobs_root.mkdir(parents=True)

    done_drift_run = jobs_root / "done-drift-run"
    done_drift_run.mkdir()
    (done_drift_run / "run.log").write_text("has output")
    (done_drift_run / "agent.log").write_text("has output")
    (done_drift_run / "run.json").write_text(
        json.dumps(
            {
                "last_state": "DONE",
                "needs_user_input": "not-a-bool",
            }
        )
    )

    result = runner.invoke(main, ["clean", "--loops-root", str(loops_root)])

    assert result.exit_code == 0, result.output
    assert not done_drift_run.exists()
    archived_done = loops_root / ".archive" / "done-drift-run"
    assert archived_done.exists()
    assert (archived_done / "run.json").exists()


def test_clean_skips_non_utf8_run_record_and_continues(tmp_path: Path) -> None:
    runner = CliRunner()
    loops_root = tmp_path / ".loops"
    jobs_root = loops_root / "jobs"
    jobs_root.mkdir(parents=True)

    broken_run = jobs_root / "broken-run"
    broken_run.mkdir()
    (broken_run / "run.log").write_text("has output")
    (broken_run / "agent.log").write_text("has output")
    (broken_run / "run.json").write_bytes(b"\xff\xfe\x00\x01")

    done_run = jobs_root / "done-run"
    done_run.mkdir()
    (done_run / "run.log").write_text("has output")
    (done_run / "agent.log").write_text("has output")
    write_run_record(
        done_run / "run.json",
        RunRecord(
            task=Task(
                provider_id="github_projects_v2",
                id="done",
                title="Done task",
                status="Done",
                url="https://github.com/acme/api/issues/4",
                created_at="2026-03-01T00:00:00Z",
                updated_at="2026-03-01T00:00:00Z",
            ),
            pr=RunPR(
                url="https://github.com/acme/api/pull/4",
                merged_at="2026-03-01T00:00:01Z",
            ),
            codex_session=None,
            needs_user_input=False,
            last_state="RUNNING",
            updated_at="",
        ),
    )

    result = runner.invoke(main, ["clean", "--loops-root", str(loops_root)])

    assert result.exit_code == 0, result.output
    assert broken_run.exists()
    assert not done_run.exists()
    archived_done = loops_root / ".archive" / "done-run"
    assert archived_done.exists()


def test_normalize_argv_preserves_known_subcommands() -> None:
    argv = ["python", "doctor"]
    assert _normalize_argv(argv) == argv
    clean_argv = ["python", "clean"]
    assert _normalize_argv(clean_argv) == clean_argv
    handoff_argv = ["python", "handoff", "session-1"]
    assert _normalize_argv(handoff_argv) == handoff_argv


def test_normalize_argv_preserves_removed_signal_subcommand() -> None:
    argv = ["python", "signal", "--message", "Need help"]
    assert _normalize_argv(argv) == argv


def test_normalize_argv_routes_legacy_flags_to_run() -> None:
    argv = ["python", "--run-once"]
    assert _normalize_argv(argv) == ["python", "run", "--run-once"]


def test_normalize_argv_defaults_to_run_when_no_args() -> None:
    argv = ["python"]
    assert _normalize_argv(argv) == ["python", "run"]


def test_entrypoint_normalizes_argv_before_invoking_click(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_main(*, prog_name: str) -> None:
        captured["prog_name"] = prog_name
        captured["argv"] = list(sys.argv)

    monkeypatch.setattr("loops.__main__.main", fake_main)

    entrypoint(["loops", "--run-once"])

    assert captured["prog_name"] == "loops"
    assert captured["argv"] == ["loops", "run", "--run-once"]


def test_removed_signal_command_errors_explicitly() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["signal", "--message", "Need help"])

    assert result.exit_code != 0
    assert "No such command 'signal'" in result.output


def test_python_module_loops_cli_is_not_available() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "loops.cli"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "No module named loops.cli" in completed.stderr


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


def test_inner_loop_reset_uses_runtime_stream_logs_stdout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = CliRunner()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    write_run_record(
        run_dir / "run.json",
        RunRecord(
            task=Task(
                provider_id="github",
                id="11",
                title="Existing task",
                status="ready",
                url="https://github.com/acme/api/issues/11",
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
    write_inner_loop_runtime_config(
        run_dir,
        InnerLoopRuntimeConfig(stream_logs_stdout=True),
    )

    def _should_not_run(*_args, **_kwargs) -> None:
        raise AssertionError("should not run")

    monkeypatch.setattr(cli_module, "run_inner_loop", _should_not_run)

    result = runner.invoke(main, ["inner-loop", "--run-dir", str(run_dir), "--reset"])

    assert result.exit_code == 0, result.output
    record = read_run_record(run_dir / "run.json")
    assert record.stream_logs_stdout is True


def test_inner_loop_reset_preserves_checkout_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = CliRunner()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    write_run_record(
        run_dir / "run.json",
        RunRecord(
            task=Task(
                provider_id="github",
                id="14",
                title="Existing task",
                status="ready",
                url="https://github.com/acme/api/issues/14",
                created_at="2026-02-09T00:00:00Z",
                updated_at="2026-02-09T00:00:00Z",
            ),
            pr=None,
            codex_session=None,
            needs_user_input=True,
            needs_user_input_payload={"message": "old prompt"},
            checkout_mode="worktree",
            starting_commit="abc123",
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
    assert record.checkout_mode == "worktree"
    assert record.starting_commit == "abc123"


def test_inner_loop_reset_clears_state_hook_ledger(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = CliRunner()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    write_run_record(
        run_dir / "run.json",
        RunRecord(
            task=Task(
                provider_id="github",
                id="15",
                title="Existing task",
                status="ready",
                url="https://github.com/acme/api/issues/15",
                created_at="2026-02-09T00:00:00Z",
                updated_at="2026-02-09T00:00:00Z",
            ),
            pr=None,
            codex_session=None,
            needs_user_input=False,
            last_state="RUNNING",
            updated_at="",
        ),
    )
    ledger_path = run_dir / STATE_HOOKS_LEDGER_FILE
    ledger_path.write_text(
        json.dumps({"executed": ["run-id:enter:RUNNING:TaskStatusHook"]}),
        encoding="utf-8",
    )

    def _should_not_run(*_args, **_kwargs) -> None:
        raise AssertionError("should not run")

    monkeypatch.setattr(cli_module, "run_inner_loop", _should_not_run)

    result = runner.invoke(main, ["inner-loop", "--run-dir", str(run_dir), "--reset"])

    assert result.exit_code == 0, result.output
    assert not ledger_path.exists()
    run_log = (run_dir / "run.log").read_text()
    assert "removed state hook ledger during reset" in run_log


def test_inner_loop_reset_runtime_stream_logs_overrides_existing_record(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = CliRunner()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    write_run_record(
        run_dir / "run.json",
        RunRecord(
            task=Task(
                provider_id="github",
                id="12",
                title="Existing task",
                status="ready",
                url="https://github.com/acme/api/issues/12",
                created_at="2026-02-09T00:00:00Z",
                updated_at="2026-02-09T00:00:00Z",
            ),
            pr=None,
            codex_session=None,
            needs_user_input=True,
            needs_user_input_payload={"message": "old prompt"},
            stream_logs_stdout=True,
            last_state="NEEDS_INPUT",
            updated_at="",
        ),
    )
    write_inner_loop_runtime_config(
        run_dir,
        InnerLoopRuntimeConfig(stream_logs_stdout=False),
    )

    def _should_not_run(*_args, **_kwargs) -> None:
        raise AssertionError("should not run")

    monkeypatch.setattr(cli_module, "run_inner_loop", _should_not_run)

    result = runner.invoke(main, ["inner-loop", "--run-dir", str(run_dir), "--reset"])

    assert result.exit_code == 0, result.output
    record = read_run_record(run_dir / "run.json")
    assert record.stream_logs_stdout is False


def test_inner_loop_reset_uses_env_stream_logs_stdout_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = CliRunner()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    write_run_record(
        run_dir / "run.json",
        RunRecord(
            task=Task(
                provider_id="github",
                id="13",
                title="Existing task",
                status="ready",
                url="https://github.com/acme/api/issues/13",
                created_at="2026-02-09T00:00:00Z",
                updated_at="2026-02-09T00:00:00Z",
            ),
            pr=None,
            codex_session=None,
            needs_user_input=True,
            needs_user_input_payload={"message": "old prompt"},
            stream_logs_stdout=True,
            last_state="NEEDS_INPUT",
            updated_at="",
        ),
    )
    monkeypatch.setenv("LOOPS_STREAM_LOGS_STDOUT", "0")

    def _should_not_run(*_args, **_kwargs) -> None:
        raise AssertionError("should not run")

    monkeypatch.setattr(cli_module, "run_inner_loop", _should_not_run)

    result = runner.invoke(main, ["inner-loop", "--run-dir", str(run_dir), "--reset"])

    assert result.exit_code == 0, result.output
    record = read_run_record(run_dir / "run.json")
    assert record.stream_logs_stdout is False


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


def test_handoff_command_passes_arguments_to_core_handler(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = CliRunner()
    config_path = tmp_path / "config.json"
    config_path.write_text("{}")
    captured: dict[str, object] = {}

    def fake_run_handoff_command(
        *,
        config_path: Path,
        session_id: str | None,
        pr_url: str | None,
        task_url: str | None,
    ) -> None:
        captured["config_path"] = config_path
        captured["session_id"] = session_id
        captured["pr_url"] = pr_url
        captured["task_url"] = task_url

    monkeypatch.setattr(cli_module, "_run_handoff_command", fake_run_handoff_command)

    result = runner.invoke(
        main,
        [
            "handoff",
            "session-1",
            "--config",
            str(config_path),
            "--pr-url",
            "https://github.com/acme/api/pull/44",
            "--task-url",
            "https://github.com/acme/api/issues/44",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["config_path"] == config_path
    assert captured["session_id"] == "session-1"
    assert captured["pr_url"] == "https://github.com/acme/api/pull/44"
    assert captured["task_url"] == "https://github.com/acme/api/issues/44"


def test_run_handoff_command_seeds_waiting_on_review_and_launches(
    tmp_path: Path,
    monkeypatch,
) -> None:
    loops_root = tmp_path / ".loops"
    loops_root.mkdir(parents=True)
    config_path = loops_root / "config.json"
    config_path.write_text("{}")

    task = Task(
        provider_id="github_projects_v2",
        id="issue-node-77",
        title="Handoff issue",
        status="Ready",
        url="https://github.com/acme/api/issues/77",
        created_at="2026-03-01T00:00:00Z",
        updated_at="2026-03-01T00:00:00Z",
        repo="acme/api",
    )
    loaded = LoopsConfig(
        version=LATEST_LOOPS_CONFIG_VERSION,
        task_provider_id="github_projects_v2",
        task_provider_config={
            "url": "https://github.com/orgs/acme/projects/7",
            "status_field": "Status",
        },
        loop_config=OuterLoopConfig(
            sync_mode=False,
            checkout_mode="worktree",
        ),
        inner_loop=None,
    )

    class ProviderStub:
        approval_comment_usernames = ("maintainer", "maintainer")
        approval_comment_pattern = r"^\s*/shipit\b"
        review_actor_allowlist = ("reviewer", "reviewer")

        def poll(self, limit: int | None = None) -> list[Task]:
            assert limit is None
            return [task]

    provider = ProviderStub()
    captured: dict[str, object] = {}

    def fake_build_inner_loop_launcher(
        config: LoopsConfig,
        *,
        approval_comment_usernames: tuple[str, ...] = (),
        approval_comment_pattern: str = r"^\s*/approve\b",
        review_actor_usernames: tuple[str, ...] = (),
    ):
        captured["launcher_config"] = config
        captured["approval_comment_usernames"] = approval_comment_usernames
        captured["approval_comment_pattern"] = approval_comment_pattern
        captured["review_actor_usernames"] = review_actor_usernames

        def _launch(run_dir: Path, launch_task: Task) -> None:
            captured["run_dir"] = run_dir
            captured["launch_task"] = launch_task

        return _launch

    monkeypatch.setattr(cli_module, "load_config", lambda _path: loaded)
    monkeypatch.setattr(cli_module, "build_provider", lambda _config: provider)
    monkeypatch.setattr(cli_module, "build_inner_loop_launcher", fake_build_inner_loop_launcher)
    monkeypatch.setattr(
        cli_module,
        "_find_codex_session_transcript",
        lambda _session_id: tmp_path / "session.jsonl",
    )
    monkeypatch.setattr(
        cli_module,
        "_derive_handoff_urls_from_session",
        lambda _path, **_kwargs: (
            "https://github.com/acme/api/pull/88",
            "https://github.com/acme/api/issues/77",
            ("https://github.com/acme/api/pull/88",),
            ("https://github.com/acme/api/issues/77",),
        ),
    )
    monkeypatch.setattr(cli_module, "_resolve_starting_commit", lambda _root: "abc123")

    cli_module._run_handoff_command(
        config_path=config_path,
        session_id="session-xyz",
        pr_url=None,
        task_url=None,
    )

    run_dir = captured.get("run_dir")
    assert isinstance(run_dir, Path)
    run_record = read_run_record(run_dir / "run.json")
    assert run_record.task.url == "https://github.com/acme/api/issues/77"
    assert run_record.pr is not None
    assert run_record.pr.url == "https://github.com/acme/api/pull/88"
    assert run_record.pr.review_status == "open"
    assert run_record.pr.review_addressed_at is None
    assert run_record.codex_session is not None
    assert run_record.codex_session.id == "session-xyz"
    assert run_record.needs_user_input is False
    assert run_record.last_state == "WAITING_ON_REVIEW"
    assert run_record.checkout_mode == "worktree"
    assert run_record.starting_commit == "abc123"
    assert (run_dir / "run.log").exists()
    assert (run_dir / "agent.log").exists()

    outer_state_payload = json.loads((loops_root / "outer_state.json").read_text())
    task_entries = list((outer_state_payload.get("tasks") or {}).values())
    assert len(task_entries) == 1
    assert task_entries[0]["task"]["url"] == "https://github.com/acme/api/issues/77"
    assert outer_state_payload["initialized"] is True
    assert captured["launch_task"] == task
    assert captured["approval_comment_usernames"] == ("maintainer",)
    assert captured["approval_comment_pattern"] == r"^\s*/shipit\b"
    assert captured["review_actor_usernames"] == ("reviewer",)


def test_run_handoff_command_falls_back_when_provider_poll_fails(
    tmp_path: Path,
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    loops_root = tmp_path / ".loops"
    loops_root.mkdir(parents=True)
    config_path = loops_root / "config.json"
    config_path.write_text("{}")
    loaded = LoopsConfig(
        version=LATEST_LOOPS_CONFIG_VERSION,
        task_provider_id="github_projects_v2",
        task_provider_config={
            "url": "https://github.com/orgs/acme/projects/7",
            "status_field": "Status",
        },
        loop_config=OuterLoopConfig(sync_mode=False),
        inner_loop=None,
    )

    class ProviderStub:
        approval_comment_usernames = ()
        approval_comment_pattern = r"^\s*/approve\b"
        review_actor_allowlist = ()

        def poll(self, limit: int | None = None) -> list[Task]:
            assert limit is None
            raise RuntimeError("poll unavailable")

    provider = ProviderStub()
    captured: dict[str, object] = {}

    def fake_build_inner_loop_launcher(
        _config: LoopsConfig,
        *,
        approval_comment_usernames: tuple[str, ...] = (),
        approval_comment_pattern: str = r"^\s*/approve\b",
        review_actor_usernames: tuple[str, ...] = (),
    ):
        captured["approval_comment_usernames"] = approval_comment_usernames
        captured["approval_comment_pattern"] = approval_comment_pattern
        captured["review_actor_usernames"] = review_actor_usernames

        def _launch(run_dir: Path, launch_task: Task) -> None:
            captured["run_dir"] = run_dir
            captured["launch_task"] = launch_task

        return _launch

    monkeypatch.setattr(cli_module, "load_config", lambda _path: loaded)
    monkeypatch.setattr(cli_module, "build_provider", lambda _config: provider)
    monkeypatch.setattr(cli_module, "build_inner_loop_launcher", fake_build_inner_loop_launcher)
    monkeypatch.setattr(cli_module, "_resolve_starting_commit", lambda _root: "abc123")
    monkeypatch.setattr(cli_module, "_find_codex_session_transcript", lambda _id: tmp_path / "session.jsonl")
    monkeypatch.setattr(
        cli_module,
        "_derive_handoff_urls_from_session",
        lambda _path, **_kwargs: (
            "https://github.com/acme/api/pull/88",
            "https://github.com/acme/api/issues/77",
            ("https://github.com/acme/api/pull/88",),
            ("https://github.com/acme/api/issues/77",),
        ),
    )

    cli_module._run_handoff_command(
        config_path=config_path,
        session_id="session-fallback",
        pr_url=None,
        task_url=None,
    )

    captured_err = capsys.readouterr().err
    assert "provider poll unavailable during handoff" in captured_err
    run_dir = captured.get("run_dir")
    assert isinstance(run_dir, Path)
    run_record = read_run_record(run_dir / "run.json")
    assert run_record.task.id == "https://github.com/acme/api/issues/77"
    assert run_record.task.url == "https://github.com/acme/api/issues/77"
    assert run_record.task.title == "Handoff task 77"
    assert run_record.codex_session is not None
    assert run_record.codex_session.id == "session-fallback"
    assert run_record.last_state == "WAITING_ON_REVIEW"
    assert captured["approval_comment_usernames"] == ()
    assert captured["approval_comment_pattern"] == r"^\s*/approve\b"
    assert captured["review_actor_usernames"] == ()


def test_build_synthesized_handoff_task_uses_canonical_url_as_id() -> None:
    task = cli_module._build_synthesized_handoff_task(
        "https://github.com/acme/api/issues/77/comments/1",
        provider_id="github_projects_v2",
    )

    assert task.id == "https://github.com/acme/api/issues/77"
    assert task.url == "https://github.com/acme/api/issues/77"
    assert task.title == "Handoff task 77"


def test_resolve_handoff_session_id_uses_codex_home_history(tmp_path: Path, monkeypatch) -> None:
    codex_home = tmp_path / "custom-codex-home"
    codex_home.mkdir(parents=True)
    history_path = codex_home / "history.jsonl"
    history_path.write_text(
        "\n".join(
            [
                json.dumps({"session_id": "older-session"}),
                json.dumps({"session_id": "latest-session"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)

    resolved = cli_module._resolve_handoff_session_id(None)

    assert resolved == "latest-session"


def test_derive_handoff_urls_from_session_ignores_tool_and_developer_content(
    tmp_path: Path,
) -> None:
    session_path = tmp_path / "session.jsonl"
    session_path.write_text(
        "\n".join(
            [
                json.dumps({"type": "session_meta", "payload": {}}),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "output": (
                                "irrelevant https://github.com/acme/api/pull/9 "
                                "https://github.com/acme/api/issues/9"
                            ),
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "developer",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "ignore https://github.com/acme/api/issues/77",
                                }
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "Opened https://github.com/kevinslin/loops/pull/80",
                                }
                            ],
                        },
                    }
                ),
                "",
            ]
        ),
        encoding="utf-8",
    )

    discovered_pr, discovered_task, pr_candidates, task_candidates = (
        cli_module._derive_handoff_urls_from_session(
            session_path,
            repo_slug="kevinslin/loops",
        )
    )

    assert discovered_pr == "https://github.com/kevinslin/loops/pull/80"
    assert discovered_task is None
    assert pr_candidates == ("https://github.com/kevinslin/loops/pull/80",)
    assert task_candidates == ()


def test_select_handoff_url_candidate_strict_repo_filters_cross_repo_candidates() -> None:
    selected, candidates = cli_module._select_handoff_url_candidate(
        [
            "https://github.com/acme/api/issues/9",
            "https://github.com/acme/api/issues/42",
        ],
        repo_slug="kevinslin/loops",
        strict_repo=True,
    )

    assert selected is None
    assert candidates == ()


def test_run_outer_loop_task_url_implies_run_once_and_force(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text("{}")
    loaded = LoopsConfig(
        version=LATEST_LOOPS_CONFIG_VERSION,
        task_provider_id="github_projects_v2",
        task_provider_config={
            "url": "https://github.com/orgs/default/projects/1",
            "status_field": "Status",
        },
        loop_config=OuterLoopConfig(force=False),
        inner_loop=None,
    )
    captured: dict[str, object] = {}

    class ProviderStub:
        approval_comment_usernames = ("Maintainer", "review-bot", "maintainer")
        approval_comment_pattern = r"^\s*/shipit\b"
        review_actor_allowlist = ("Reviewer", "review-bot", "reviewer")

    provider = ProviderStub()
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
        captured["task_provider_config"] = dict(config.task_provider_config)
        captured["inner_loop_command"] = (
            list(config.inner_loop.command) if config.inner_loop is not None else None
        )
        return provider

    def fake_build_inner_loop_launcher(
        config: LoopsConfig,
        *,
        approval_comment_usernames: tuple[str, ...] = (),
        approval_comment_pattern: str = r"^\s*/approve\b",
        review_actor_usernames: tuple[str, ...] = (),
    ) -> object:
        captured["launcher_sync_mode"] = config.loop_config.sync_mode
        captured["approval_comment_usernames"] = approval_comment_usernames
        captured["approval_comment_pattern"] = approval_comment_pattern
        captured["review_actor_usernames"] = review_actor_usernames
        return launcher

    monkeypatch.setattr(cli_module, "load_config", lambda _path: loaded)
    monkeypatch.setattr(cli_module, "build_provider", fake_build_provider)
    monkeypatch.setattr(cli_module, "build_inner_loop_launcher", fake_build_inner_loop_launcher)
    monkeypatch.setattr(cli_module, "OuterLoopRunner", FakeRunner)

    cli_module._run_outer_loop(
        config_path=config_path,
        run_once=False,
        limit=7,
        force=False,
        task_url="https://github.com/acme/api/issues/9",
    )

    assert captured["task_provider_config"] == {
        "url": "https://github.com/orgs/default/projects/1",
        "status_field": "Status",
    }
    assert captured["inner_loop_command"] == [sys.executable, "-m", "loops", "inner-loop"]
    loop_config = captured["config_arg"]
    assert isinstance(loop_config, OuterLoopConfig)
    assert loop_config.force is True
    assert loop_config.sync_mode is True
    assert captured["launcher_sync_mode"] is True
    assert captured["provider_arg"] is provider
    assert captured["inner_loop_launcher"] is launcher
    assert captured["run_once_limit"] == 7
    assert captured["run_once_task_url"] == "https://github.com/acme/api/issues/9"
    assert captured["approval_comment_usernames"] == ("maintainer", "review-bot")
    assert captured["approval_comment_pattern"] == r"^\s*/shipit\b"
    assert captured["review_actor_usernames"] == ("reviewer", "review-bot")
    assert "run_forever_limit" not in captured


def test_run_outer_loop_sync_mode_interrupt_prints_run_resume_command(
    tmp_path: Path,
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text("{}")
    run_dir = tmp_path / ".loops" / "jobs" / "2026-03-01-ship-it-1"
    loaded = LoopsConfig(
        version=LATEST_LOOPS_CONFIG_VERSION,
        task_provider_id="github_projects_v2",
        task_provider_config={},
        loop_config=OuterLoopConfig(sync_mode=True),
        inner_loop=None,
    )

    class FakeRunner:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def run_once(
            self,
            *,
            limit: int | None = None,
            forced_task_url: str | None = None,
        ) -> None:
            raise SyncModeInterruptedError(run_dir=run_dir)

        def run_forever(self, *, limit: int | None = None) -> None:
            raise AssertionError("run_forever should not be called")

    monkeypatch.setattr(cli_module, "load_config", lambda _path: loaded)
    monkeypatch.setattr(cli_module, "build_provider", lambda _config: object())
    monkeypatch.setattr(
        cli_module,
        "build_inner_loop_launcher",
        lambda _config, **_kwargs: object(),
    )
    monkeypatch.setattr(cli_module, "OuterLoopRunner", FakeRunner)

    with pytest.raises(click.Abort):
        cli_module._run_outer_loop(
            config_path=config_path,
            run_once=True,
            limit=None,
            force=None,
            task_url=None,
        )

    output = capsys.readouterr().out
    assert "Sync mode interrupted." in output
    assert f"loops inner-loop --run-dir {run_dir}" in output


def test_run_outer_loop_non_launcher_interrupt_does_not_print_resume_hint(
    tmp_path: Path,
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text("{}")
    loaded = LoopsConfig(
        version=LATEST_LOOPS_CONFIG_VERSION,
        task_provider_id="github_projects_v2",
        task_provider_config={},
        loop_config=OuterLoopConfig(sync_mode=True),
        inner_loop=None,
    )

    class FakeRunner:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def run_once(
            self,
            *,
            limit: int | None = None,
            forced_task_url: str | None = None,
        ) -> None:
            raise KeyboardInterrupt

        def run_forever(self, *, limit: int | None = None) -> None:
            raise AssertionError("run_forever should not be called")

    monkeypatch.setattr(cli_module, "load_config", lambda _path: loaded)
    monkeypatch.setattr(cli_module, "build_provider", lambda _config: object())
    monkeypatch.setattr(
        cli_module,
        "build_inner_loop_launcher",
        lambda _config, **_kwargs: object(),
    )
    monkeypatch.setattr(cli_module, "OuterLoopRunner", FakeRunner)

    with pytest.raises(click.Abort):
        cli_module._run_outer_loop(
            config_path=config_path,
            run_once=True,
            limit=None,
            force=None,
            task_url=None,
        )

    output = capsys.readouterr().out
    assert output == ""


def test_doctor_upgrades_legacy_config(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "version": 1,
                "provider_id": "github_projects_v2",
                "provider_config": {"url": "https://github.com/orgs/acme/projects/7"},
                "loop_config": {"task_ready_status": "Todo"},
            }
        )
    )

    result = runner.invoke(main, ["doctor", "--config", str(config_path)])

    assert result.exit_code == 0, result.output
    assert "Upgraded config to version" in result.output
    payload = json.loads(config_path.read_text())
    assert payload["version"] == LATEST_LOOPS_CONFIG_VERSION
    assert "provider_id" not in payload
    assert "provider_config" not in payload
    assert payload["task_provider_id"] == "github_projects_v2"
    assert payload["task_provider_config"] == {
        "approval_comment_pattern": r"^\s*/approve\b",
        "approval_comment_usernames": [],
        "allowlist": [],
        "page_size": 50,
        "status_field": "Status",
        "url": "https://github.com/orgs/acme/projects/7",
    }
    assert payload["loop_config"]["task_ready_status"] == "Todo"
    assert payload["loop_config"]["parallel_tasks"] is False
    assert payload["loop_config"]["auto_approve_enabled"] is False
    assert payload["loop_config"]["handoff_handler"] == "stdin_handler"
    assert payload["loop_config"]["checkout_mode"] == "branch"


def test_doctor_moves_legacy_loop_approval_keys_to_provider_config(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "version": 2,
                "task_provider_id": "github_projects_v2",
                "task_provider_config": {
                    "url": "https://github.com/orgs/acme/projects/9",
                },
                "loop_config": {
                    "approval_comment_usernames": ["Maintainer", "review-bot"],
                    "approval_comment_pattern": r"^\s*/shipit\b",
                },
            }
        )
    )

    result = runner.invoke(main, ["doctor", "--config", str(config_path)])

    assert result.exit_code == 0, result.output
    payload = json.loads(config_path.read_text())
    assert payload["version"] == LATEST_LOOPS_CONFIG_VERSION
    assert payload["task_provider_config"]["approval_comment_usernames"] == [
        "Maintainer",
        "review-bot",
    ]
    assert payload["task_provider_config"]["approval_comment_pattern"] == r"^\s*/shipit\b"
    assert "approval_comment_usernames" not in payload["loop_config"]
    assert "approval_comment_pattern" not in payload["loop_config"]


def test_doctor_migrates_legacy_inner_loop_module_command(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "version": LATEST_LOOPS_CONFIG_VERSION,
                "task_provider_id": "github_projects_v2",
                "task_provider_config": {"url": "https://github.com/orgs/acme/projects/7"},
                "inner_loop": {
                    "command": ["python3", "-m", "loops.inner_loop"],
                    "append_task_url": False,
                },
            }
        )
    )

    result = runner.invoke(main, ["doctor", "--config", str(config_path)])

    assert result.exit_code == 0, result.output
    payload = json.loads(config_path.read_text())
    assert payload["inner_loop"]["command"] == [
        "python3",
        "-m",
        "loops",
        "inner-loop",
    ]


def test_doctor_upgrades_versionless_legacy_config(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "provider_id": "github_projects_v2",
                "provider_config": {"url": "https://github.com/orgs/acme/projects/8"},
            }
        )
    )

    result = runner.invoke(main, ["doctor", "--config", str(config_path)])

    assert result.exit_code == 0, result.output
    payload = json.loads(config_path.read_text())
    assert payload["version"] == LATEST_LOOPS_CONFIG_VERSION
    assert "provider_id" not in payload
    assert "provider_config" not in payload
    assert payload["task_provider_id"] == "github_projects_v2"
    assert payload["task_provider_config"] == {
        "approval_comment_pattern": r"^\s*/approve\b",
        "approval_comment_usernames": [],
        "allowlist": [],
        "page_size": 50,
        "status_field": "Status",
        "url": "https://github.com/orgs/acme/projects/8",
    }


def test_doctor_reports_when_config_is_up_to_date(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(cli_module._build_default_config()))

    result = runner.invoke(main, ["doctor", "--config", str(config_path)])

    assert result.exit_code == 0, result.output
    assert "Config already up to date" in result.output


def test_doctor_does_not_synthesize_missing_github_provider_url(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "task_provider_id": "github_projects_v2",
                "task_provider_config": {},
            }
        )
    )

    result = runner.invoke(main, ["doctor", "--config", str(config_path)])

    assert result.exit_code == 0, result.output
    payload = json.loads(config_path.read_text())
    assert payload["task_provider_config"] == {
        "approval_comment_pattern": r"^\s*/approve\b",
        "approval_comment_usernames": [],
        "allowlist": [],
        "page_size": 50,
        "status_field": "Status",
    }


def test_loop_config_defaults_are_consistent_across_entrypoints(tmp_path: Path) -> None:
    expected_defaults = build_default_loop_config_payload()
    runner = CliRunner()

    loops_root = tmp_path / ".loops"
    init_result = runner.invoke(main, ["init", "--loops-root", str(loops_root)])
    assert init_result.exit_code == 0, init_result.output
    init_payload = json.loads((loops_root / "config.json").read_text())
    assert init_payload["loop_config"] == expected_defaults

    doctor_config_path = tmp_path / "doctor-config.json"
    doctor_config_path.write_text(
        json.dumps(
            {
                "task_provider_id": "github_projects_v2",
                "task_provider_config": {"url": "https://github.com/orgs/acme/projects/7"},
            }
        )
    )
    doctor_result = runner.invoke(main, ["doctor", "--config", str(doctor_config_path)])
    assert doctor_result.exit_code == 0, doctor_result.output
    doctor_payload = json.loads(doctor_config_path.read_text())
    assert doctor_payload["loop_config"] == expected_defaults

    loader_config_path = tmp_path / "loader-config.json"
    loader_config_path.write_text(
        json.dumps(
            {
                "task_provider_id": "github_projects_v2",
                "task_provider_config": {},
            }
        )
    )
    loaded = load_config(loader_config_path)
    loaded_defaults = asdict(loaded.loop_config)
    assert loaded_defaults == expected_defaults


def test_default_provider_and_inner_loop_payloads_are_canonical() -> None:
    default_config = cli_module._build_default_config()
    assert default_config["task_provider_config"] == build_default_provider_config_payload()
    assert default_config["inner_loop"] == cli_module._build_default_inner_loop_payload()
