from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.integ.github_setup import (
    LOOPS_INTEG_PROJECT_URL,
    LOOPS_INTEG_REPO,
    cleanup_live_issue_bundle,
    create_live_issue_bundle,
    require_github_token,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FAST_CODEX_CMD = os.environ.get(
    "LOOPS_INTEG_CODEX_CMD",
    'codex exec --dangerously-bypass-approvals-and-sandbox '
    '"Reply with exactly OK and no extra text."',
)
DEFAULT_POLL_ATTEMPTS = 3
DEFAULT_POLL_DELAY_SECONDS = 2.0


pytestmark = pytest.mark.skipif(
    os.environ.get("LOOPS_INTEG_LIVE") != "1",
    reason="Set LOOPS_INTEG_LIVE=1 to run live integration tests.",
)


def test_outer_loop_pickup_live(tmp_path: Path) -> None:
    require_binary("gh")
    require_binary("codex")
    token = require_github_token()
    bundle = None
    cleanup_error: Exception | None = None

    try:
        bundle = create_live_issue_bundle(token=token)
        loops_root = tmp_path / ".loops"
        config_path = loops_root / "config.json"
        write_live_config(config_path=config_path, run_label=bundle.run_label)

        env = os.environ.copy()
        env["GITHUB_TOKEN"] = token
        env["GH_TOKEN"] = token
        env["PYTHONPATH"] = build_pythonpath(REPO_ROOT, env.get("PYTHONPATH"))

        timeout_seconds = int(os.environ.get("LOOPS_INTEG_TIMEOUT_SECONDS", "300"))
        command = [
            sys.executable,
            "-m",
            "loops",
            "run",
            "--run-once",
            "--limit",
            "1",
            "--config",
            str(config_path),
        ]
        run_dir, _result = run_until_single_run_dir(
            command=command,
            cwd=REPO_ROOT,
            env=env,
            loops_root=loops_root,
            timeout_seconds=timeout_seconds,
        )
        run_record = json.loads((run_dir / "run.json").read_text())
        assert run_record["task"]["title"] == bundle.task1.title
        assert run_record["needs_user_input"] is True
        payload = run_record.get("needs_user_input_payload") or {}
        message = payload.get("message")
        assert isinstance(message, str)
        assert "without opening a PR" in message

        run_log = (run_dir / "run.log").read_text()
        assert "[loops] codex turn: starting new codex session" in run_log
        assert "[loops] codex invocation failed" not in run_log
        assert "[loops] codex exit code " not in run_log
    finally:
        if bundle is not None:
            try:
                cleanup_live_issue_bundle(bundle, token=token)
            except Exception as exc:  # pragma: no cover - defensive cleanup
                cleanup_error = exc

    if cleanup_error is not None:
        raise cleanup_error


def write_live_config(*, config_path: Path, run_label: str) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "provider_id": "github_projects_v2",
        "provider_config": {
            "url": LOOPS_INTEG_PROJECT_URL,
            "status_field": "Status",
            "filters": [
                f"repository={LOOPS_INTEG_REPO}",
                f"tag={run_label}",
            ],
        },
        "loop_config": {
            "emit_on_first_run": True,
            "force": True,
            "sync_mode": True,
            "task_ready_status": "Todo",
        },
        "inner_loop": {
            "command": [sys.executable, "-m", "loops.inner_loop"],
            "working_dir": str(REPO_ROOT),
            "env": {
                "PYTHONPATH": str(REPO_ROOT),
                "CODEX_CMD": FAST_CODEX_CMD,
            },
            "append_task_url": False,
        },
    }
    config_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def run_until_single_run_dir(
    *,
    command: list[str],
    cwd: Path,
    env: dict[str, str],
    loops_root: Path,
    timeout_seconds: int,
) -> tuple[Path, subprocess.CompletedProcess[str]]:
    attempts = int(
        os.environ.get("LOOPS_INTEG_POLL_ATTEMPTS", str(DEFAULT_POLL_ATTEMPTS))
    )
    delay_seconds = float(
        os.environ.get("LOOPS_INTEG_POLL_DELAY_SECONDS", str(DEFAULT_POLL_DELAY_SECONDS))
    )
    if attempts <= 0:
        raise ValueError("LOOPS_INTEG_POLL_ATTEMPTS must be positive")

    last_result: subprocess.CompletedProcess[str] | None = None
    runs_root = loops_root / "jobs"
    for attempt in range(1, attempts + 1):
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        last_result = result
        assert result.returncode == 0, (
            "loops run command failed\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )

        if runs_root.exists():
            run_dirs = sorted(path for path in runs_root.iterdir() if path.is_dir())
            if len(run_dirs) == 1:
                return run_dirs[0], result
            assert len(run_dirs) == 0, f"expected at most 1 run dir, got {run_dirs}"

        if attempt < attempts:
            time.sleep(delay_seconds)

    assert last_result is not None
    raise AssertionError(
        "expected exactly 1 run dir after retries, got none\n"
        f"last stdout:\n{last_result.stdout}\n"
        f"last stderr:\n{last_result.stderr}"
    )


def require_binary(binary: str) -> None:
    if shutil.which(binary) is None:
        pytest.skip(f"{binary} is required for live integration test")


def build_pythonpath(repo_root: Path, existing: str | None) -> str:
    if existing is None or not existing.strip():
        return str(repo_root)
    return f"{repo_root}{os.pathsep}{existing}"
