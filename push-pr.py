#!/usr/bin/env python3
"""Push current branch and create a pull request.

The script prints only the PR URL to stdout on success.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

PR_TITLE = "[feat|enhance|chore|fix|docs]: [description of change]"
PR_BODY_PATH = Path("/tmp/pr_body.md")
PR_BODY_TEMPLATE = """[feat|enhance|chore|fix|docs]: [description of change]

## Context
[what this change does]

## Testing
[description of tests]
"""
PR_URL_PATTERN = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[0-9]+")


def _run_capture(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        text=True,
        capture_output=True,
    )


def _run_stream(command: list[str]) -> None:
    subprocess.run(
        command,
        check=True,
        text=True,
        stdout=sys.stderr,
        stderr=sys.stderr,
    )


def _extract_pr_url(text: str) -> str | None:
    match = PR_URL_PATTERN.search(text)
    return match.group(0) if match is not None else None


def _current_branch() -> str:
    result = _run_capture(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    branch = result.stdout.strip()
    if result.returncode != 0 or not branch:
        raise RuntimeError("Unable to determine current git branch.")
    return branch


def _has_upstream() -> bool:
    result = _run_capture(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    return result.returncode == 0 and bool(result.stdout.strip())


def _push_code() -> None:
    branch = _current_branch()
    if _has_upstream():
        _run_stream(["git", "push"])
    else:
        _run_stream(["git", "push", "-u", "origin", branch])


def _resolve_base_branch() -> str:
    gh_result = _run_capture(
        [
            "gh",
            "repo",
            "view",
            "--json",
            "defaultBranchRef",
            "--jq",
            ".defaultBranchRef.name",
        ]
    )
    gh_base = gh_result.stdout.strip()
    if gh_result.returncode == 0 and gh_base:
        return gh_base

    git_result = _run_capture(["git", "symbolic-ref", "refs/remotes/origin/HEAD"])
    git_ref = git_result.stdout.strip()
    prefix = "refs/remotes/origin/"
    if git_result.returncode == 0 and git_ref.startswith(prefix):
        return git_ref[len(prefix) :]

    raise RuntimeError("Unable to determine default base branch")


def _write_pr_body() -> None:
    PR_BODY_PATH.write_text(PR_BODY_TEMPLATE, encoding="utf-8")


def _create_pr(base_branch: str) -> str:
    result = _run_capture(
        [
            "gh",
            "pr",
            "create",
            "--base",
            base_branch,
            "--title",
            PR_TITLE,
            "--body-file",
            str(PR_BODY_PATH),
        ]
    )
    combined_output = "\n".join(
        part for part in (result.stdout.strip(), result.stderr.strip()) if part
    )
    pr_url = _extract_pr_url(combined_output)
    if pr_url is not None:
        return pr_url

    if combined_output:
        print(combined_output, file=sys.stderr)
    raise RuntimeError("Unable to determine PR URL from gh pr create output.")


def main() -> int:
    try:
        _push_code()
        _write_pr_body()
        base_branch = _resolve_base_branch()
        pr_url = _create_pr(base_branch)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(pr_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
