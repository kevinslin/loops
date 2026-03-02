#!/usr/bin/env python3
from __future__ import annotations

"""Create an initial PR and persist its URL for deterministic inner-loop discovery."""

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys

PUSH_PR_URL_FILE = "push-pr.url"
PR_URL_PATTERN = re.compile(
    r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[0-9]+"
)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create PR and persist URL to LOOPS_RUN_DIR/push-pr.url"
    )
    parser.add_argument("pr_title", help="Title passed to gh pr create --title")
    parser.add_argument(
        "pr_body_file",
        help="Path where the PR body template is written, then used with --body-file",
    )
    return parser.parse_args(argv)


def _require_loops_run_dir() -> Path:
    raw = os.environ.get("LOOPS_RUN_DIR", "").strip()
    if not raw:
        raise RuntimeError("LOOPS_RUN_DIR is required")
    run_dir = Path(raw).expanduser().resolve()
    if not run_dir.exists():
        raise RuntimeError(f"LOOPS_RUN_DIR does not exist: {run_dir}")
    return run_dir


def _write_body_template(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[feat|enhance|chore|fix|docs]: [description of change]\n\n"
        "## Context\n"
        "[what this change does]\n\n"
        "## Testing\n"
        "[description of tests]\n",
        encoding="utf-8",
    )


def _run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _resolve_base_branch() -> str:
    base_branch = ""
    gh_lookup = _run_command(
        ["gh", "repo", "view", "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name"]
    )
    if gh_lookup.returncode == 0:
        base_branch = gh_lookup.stdout.strip()

    if not base_branch:
        symbolic_ref = _run_command(["git", "symbolic-ref", "refs/remotes/origin/HEAD"])
        if symbolic_ref.returncode == 0:
            base_branch = symbolic_ref.stdout.strip()
            prefix = "refs/remotes/origin/"
            if base_branch.startswith(prefix):
                base_branch = base_branch[len(prefix) :]

    if not base_branch:
        raise RuntimeError("Unable to determine default base branch")
    return base_branch


def _create_pr(*, title: str, body_file: Path, base_branch: str) -> str:
    created = _run_command(
        [
            "gh",
            "pr",
            "create",
            "--base",
            base_branch,
            "--title",
            title,
            "--body-file",
            str(body_file),
        ]
    )
    if created.returncode != 0:
        raise RuntimeError(
            "gh pr create failed: "
            f"{created.stderr.strip() or created.stdout.strip() or 'unknown error'}"
        )

    combined_output = "\n".join(
        chunk.strip() for chunk in (created.stdout, created.stderr) if chunk.strip()
    )
    match = PR_URL_PATTERN.search(combined_output)
    if match is None:
        raise RuntimeError("gh pr create did not return a PR URL")
    return match.group(0)


def _write_pr_url_artifact(run_dir: Path, pr_url: str) -> Path:
    artifact_path = run_dir / PUSH_PR_URL_FILE
    artifact_path.write_text(f"{pr_url}\n", encoding="utf-8")
    return artifact_path


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parse_args(sys.argv[1:] if argv is None else argv)
        run_dir = _require_loops_run_dir()
        body_file = Path(args.pr_body_file).expanduser()

        _write_body_template(body_file)
        base_branch = _resolve_base_branch()
        pr_url = _create_pr(
            title=args.pr_title,
            body_file=body_file,
            base_branch=base_branch,
        )
        artifact_path = _write_pr_url_artifact(run_dir, pr_url)

        print(pr_url)
        print(f"Wrote PR URL to {artifact_path}", file=sys.stderr)
        return 0
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
