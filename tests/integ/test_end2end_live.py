from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

from tests.integ.github_setup import (
    END2END_DEFAULT_ANIMAL,
    LOOPS_INTEG_REPO,
    cleanup_end2end_issue_bundle,
    create_end2end_issue_bundle,
    fetch_project_item_status_option_id,
    fetch_pull_request,
    require_github_token,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
INTEG_ROOT = REPO_ROOT / ".integ"
INTEG_REPO_DIR = INTEG_ROOT / "loops-integ"
INTEG_REPO_URL = f"https://github.com/{LOOPS_INTEG_REPO}.git"
END2END_CODEX_CMD = os.environ.get(
    "LOOPS_INTEG_END2END_CODEX_CMD",
    "codex exec --dangerously-bypass-approvals-and-sandbox",
)
DEFAULT_TIMEOUT_SECONDS = 900
DEFAULT_POLL_ATTEMPTS = 2
DEFAULT_POLL_DELAY_SECONDS = 5.0


pytestmark = pytest.mark.skipif(
    os.environ.get("LOOPS_INTEG_END2END") != "1",
    reason="Set LOOPS_INTEG_END2END=1 to run end-to-end live integration test.",
)


@dataclass(frozen=True)
class MergedPullRequest:
    repo: str
    number: int
    merge_commit_sha: str


def test_end2end_live() -> None:
    require_binary("gh")
    require_binary("git")
    require_binary("codex")
    token = require_github_token()
    bundle = None
    merged_pr: MergedPullRequest | None = None
    primary_error: Exception | None = None
    cleanup_errors: list[str] = []

    animal = os.environ.get("LOOPS_INTEG_END2END_ANIMAL", END2END_DEFAULT_ANIMAL)

    try:
        bootstrap_integ_repo()
        bundle = create_end2end_issue_bundle(token=token, animal=animal)
        loops_root = INTEG_REPO_DIR / ".loops"
        config_path = loops_root / "config.json"
        write_end2end_config(config_path=config_path, run_label=bundle.run_label)

        env = build_run_env(token=token)
        timeout_seconds = int(
            os.environ.get("LOOPS_INTEG_END2END_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
        )
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
            cwd=INTEG_REPO_DIR,
            env=env,
            loops_root=loops_root,
            timeout_seconds=timeout_seconds,
            run_label=bundle.run_label,
        )
        run_record = json.loads((run_dir / "run.json").read_text())

        assert run_record.get("task", {}).get("title") == bundle.task.title
        assert run_record.get("last_state") == "DONE"

        pr_payload = run_record.get("pr") or {}
        pr_url = pr_payload.get("url")
        assert isinstance(pr_url, str) and pr_url
        pr_number = pr_payload.get("number")
        assert isinstance(pr_number, int)
        pr_repo = pr_payload.get("repo")
        if not isinstance(pr_repo, str) or not pr_repo:
            pr_repo = LOOPS_INTEG_REPO
        merged_at = pr_payload.get("merged_at")
        assert isinstance(merged_at, str) and merged_at

        auto_approve = run_record.get("auto_approve") or {}
        assert auto_approve.get("verdict") == "APPROVE"

        assert bundle.task.item_id is not None
        item_status_option_id = fetch_project_item_status_option_id(
            item_id=bundle.task.item_id,
            token=token,
        )
        assert item_status_option_id == bundle.project.completed_option_id

        pr_details = fetch_pull_request(
            repo=pr_repo,
            pull_number=pr_number,
            token=token,
        )
        merged_at_remote = pr_details.get("merged_at")
        assert isinstance(merged_at_remote, str) and merged_at_remote
        merge_commit_sha = pr_details.get("merge_commit_sha")
        assert isinstance(merge_commit_sha, str) and merge_commit_sha
        merged_pr = MergedPullRequest(
            repo=pr_repo,
            number=pr_number,
            merge_commit_sha=merge_commit_sha,
        )
    except Exception as exc:  # pragma: no cover - exercised in live mode
        primary_error = exc
    finally:
        if merged_pr is not None:
            try:
                revert_merged_change(
                    repo_dir=INTEG_REPO_DIR,
                    merge_commit_sha=merged_pr.merge_commit_sha,
                )
            except Exception as exc:  # pragma: no cover - defensive cleanup logging
                cleanup_errors.append(f"failed reverting merged PR #{merged_pr.number}: {exc}")

        try:
            run_loops_clean(loops_root=INTEG_REPO_DIR / ".loops")
        except Exception as exc:  # pragma: no cover - defensive cleanup logging
            cleanup_errors.append(f"failed loops clean teardown: {exc}")

        if bundle is not None:
            try:
                cleanup_end2end_issue_bundle(bundle, token=token)
            except Exception as exc:  # pragma: no cover - defensive cleanup logging
                cleanup_errors.append(f"failed issue cleanup: {exc}")

    if primary_error is not None:
        if cleanup_errors:
            raise AssertionError(
                f"{primary_error}\ncleanup_errors:\n- " + "\n- ".join(cleanup_errors)
            ) from primary_error
        raise primary_error

    if cleanup_errors:
        raise RuntimeError("cleanup failed:\n- " + "\n- ".join(cleanup_errors))


def write_end2end_config(*, config_path: Path, run_label: str) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 4,
        "task_provider_id": "github_projects_v2",
        "task_provider_config": {
            "url": "https://github.com/users/kevinslin/projects/6/views/1",
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
            "auto_approve_enabled": True,
        },
        "inner_loop": {
            "command": [sys.executable, "-m", "loops.inner_loop"],
            "working_dir": str(INTEG_REPO_DIR),
            "env": {
                "PYTHONPATH": str(REPO_ROOT),
                "CODEX_CMD": END2END_CODEX_CMD,
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
    run_label: str,
) -> tuple[Path, subprocess.CompletedProcess[str]]:
    attempts = int(
        os.environ.get(
            "LOOPS_INTEG_END2END_POLL_ATTEMPTS",
            str(DEFAULT_POLL_ATTEMPTS),
        )
    )
    delay_seconds = float(
        os.environ.get(
            "LOOPS_INTEG_END2END_POLL_DELAY_SECONDS",
            str(DEFAULT_POLL_DELAY_SECONDS),
        )
    )
    if attempts <= 0:
        raise ValueError("LOOPS_INTEG_END2END_POLL_ATTEMPTS must be positive")

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
            run_dirs = sorted(
                path
                for path in runs_root.iterdir()
                if path.is_dir() and run_label in path.name
            )
            if len(run_dirs) == 1:
                return run_dirs[0], result
            assert len(run_dirs) == 0, (
                f"expected at most 1 run dir for run_label={run_label!r}, got {run_dirs}"
            )

        if attempt < attempts:
            time.sleep(delay_seconds)

    assert last_result is not None
    raise AssertionError(
        "expected exactly 1 run dir after retries, got none\n"
        f"last stdout:\n{last_result.stdout}\n"
        f"last stderr:\n{last_result.stderr}"
    )


def bootstrap_integ_repo() -> None:
    INTEG_ROOT.mkdir(parents=True, exist_ok=True)
    if not INTEG_REPO_DIR.exists():
        run_command(["git", "clone", INTEG_REPO_URL, str(INTEG_REPO_DIR)], cwd=REPO_ROOT)
    if not (INTEG_REPO_DIR / ".git").exists():
        raise RuntimeError(f"Expected a git repo at {INTEG_REPO_DIR}")

    default_branch = sync_repo_to_default_branch(INTEG_REPO_DIR)
    run_command(
        [
            sys.executable,
            "-m",
            "loops",
            "init",
            "--loops-root",
            ".loops",
            "--force",
        ],
        cwd=INTEG_REPO_DIR,
    )
    run_command(
        ["git", "checkout", default_branch],
        cwd=INTEG_REPO_DIR,
    )


def sync_repo_to_default_branch(repo_dir: Path) -> str:
    run_command(["git", "fetch", "origin"], cwd=repo_dir)
    default_branch = resolve_default_branch(repo_dir)
    run_command(["git", "checkout", default_branch], cwd=repo_dir)
    run_command(["git", "reset", "--hard", f"origin/{default_branch}"], cwd=repo_dir)
    return default_branch


def resolve_default_branch(repo_dir: Path) -> str:
    result = subprocess.run(
        ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        raw = result.stdout.strip()
        if raw.startswith("origin/"):
            branch = raw.removeprefix("origin/").strip()
            if branch:
                return branch
    return "main"


def revert_merged_change(*, repo_dir: Path, merge_commit_sha: str) -> None:
    default_branch = sync_repo_to_default_branch(repo_dir)
    parent_count = get_commit_parent_count(repo_dir=repo_dir, commit_sha=merge_commit_sha)
    if parent_count > 1:
        run_command(
            ["git", "revert", "--no-edit", "-m", "1", merge_commit_sha],
            cwd=repo_dir,
        )
    else:
        run_command(["git", "revert", "--no-edit", merge_commit_sha], cwd=repo_dir)
    run_command(["git", "push", "origin", default_branch], cwd=repo_dir)


def get_commit_parent_count(*, repo_dir: Path, commit_sha: str) -> int:
    result = run_command(
        ["git", "show", "--no-patch", "--format=%P", commit_sha],
        cwd=repo_dir,
    )
    parents = [part for part in result.stdout.strip().split() if part]
    return len(parents)


def run_loops_clean(*, loops_root: Path) -> None:
    run_command(
        [
            sys.executable,
            "-m",
            "loops",
            "clean",
            "--loops-root",
            str(loops_root),
        ],
        cwd=INTEG_REPO_DIR,
    )


def run_command(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env is not None:
        merged_env.update(env)
    merged_env["PYTHONPATH"] = build_pythonpath(REPO_ROOT, merged_env.get("PYTHONPATH"))
    result = subprocess.run(
        command,
        cwd=cwd,
        env=merged_env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "command failed "
            f"(exit_code={result.returncode}) command={command!r} cwd={str(cwd)!r}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def require_binary(binary: str) -> None:
    if shutil.which(binary) is None:
        pytest.skip(f"{binary} is required for live integration test")


def build_pythonpath(repo_root: Path, existing: str | None) -> str:
    if existing is None or not existing.strip():
        return str(repo_root)
    return f"{repo_root}{os.pathsep}{existing}"


def build_run_env(*, token: str) -> dict[str, str]:
    env = os.environ.copy()
    env["GITHUB_TOKEN"] = token
    env["GH_TOKEN"] = token
    env["PYTHONPATH"] = build_pythonpath(REPO_ROOT, env.get("PYTHONPATH"))
    return env
