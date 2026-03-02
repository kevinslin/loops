from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "push-pr.py"


def _load_push_pr_module():
    spec = importlib.util.spec_from_file_location("push_pr_script", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_push_pr_script_writes_body_and_artifact(tmp_path: Path, monkeypatch) -> None:
    module = _load_push_pr_module()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("LOOPS_RUN_DIR", str(run_dir))
    body_file = tmp_path / "pr_body.md"

    calls: list[list[str]] = []

    def fake_run(command, check, stdout, stderr, text):
        del check, stdout, stderr, text
        calls.append(command)
        if command[:3] == ["gh", "repo", "view"]:
            return subprocess.CompletedProcess(command, 0, "main\n", "")
        if command[:3] == ["gh", "pr", "create"]:
            return subprocess.CompletedProcess(
                command,
                0,
                "https://github.com/acme/api/pull/99\n",
                "",
            )
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    exit_code = module.main(["feat: deterministic pr discovery", str(body_file)])

    assert exit_code == 0
    assert body_file.read_text(encoding="utf-8") == (
        "[feat|enhance|chore|fix|docs]: [description of change]\n\n"
        "## Context\n"
        "[what this change does]\n\n"
        "## Testing\n"
        "[description of tests]\n"
    )
    assert (run_dir / module.PUSH_PR_URL_FILE).read_text(encoding="utf-8") == (
        "https://github.com/acme/api/pull/99\n"
    )
    assert calls[1][:6] == [
        "gh",
        "pr",
        "create",
        "--base",
        "main",
        "--title",
    ]


def test_push_pr_script_falls_back_to_origin_head(tmp_path: Path, monkeypatch) -> None:
    module = _load_push_pr_module()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("LOOPS_RUN_DIR", str(run_dir))
    body_file = tmp_path / "pr_body.md"

    calls: list[list[str]] = []

    def fake_run(command, check, stdout, stderr, text):
        del check, stdout, stderr, text
        calls.append(command)
        if command[:3] == ["gh", "repo", "view"]:
            return subprocess.CompletedProcess(command, 1, "", "gh unavailable")
        if command[:3] == ["git", "symbolic-ref", "refs/remotes/origin/HEAD"]:
            return subprocess.CompletedProcess(
                command,
                0,
                "refs/remotes/origin/develop\n",
                "",
            )
        if command[:3] == ["gh", "pr", "create"]:
            return subprocess.CompletedProcess(
                command,
                0,
                "https://github.com/acme/api/pull/101\n",
                "",
            )
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    exit_code = module.main(["feat: fallback base branch", str(body_file)])

    assert exit_code == 0
    assert "--base" in calls[2]
    assert "develop" in calls[2]


def test_push_pr_script_requires_loops_run_dir(tmp_path: Path, monkeypatch) -> None:
    module = _load_push_pr_module()
    monkeypatch.delenv("LOOPS_RUN_DIR", raising=False)
    body_file = tmp_path / "pr_body.md"

    exit_code = module.main(["feat: missing run dir", str(body_file)])
    assert exit_code == 1

