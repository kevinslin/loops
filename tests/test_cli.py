from __future__ import annotations

import json
import sys
from pathlib import Path

from click.testing import CliRunner

from loops.__main__ import _normalize_argv
from loops.cli import main


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
