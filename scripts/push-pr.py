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


def _resolve_repo_root(run_dir: Path) -> Path:
    repo_lookup = _run_command(["git", "rev-parse", "--show-toplevel"], cwd=run_dir)
    if repo_lookup.returncode != 0:
        raise RuntimeError("Unable to determine repository root from LOOPS_RUN_DIR")
    repo_root_raw = repo_lookup.stdout.strip()
    if not repo_root_raw:
        raise RuntimeError("Unable to determine repository root from LOOPS_RUN_DIR")
    return Path(repo_root_raw).expanduser().resolve()


def _resolve_head_branch(repo_root: Path) -> str:
    branch_lookup = _run_command(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_root,
    )
    if branch_lookup.returncode != 0:
        return "unknown-branch"
    branch = branch_lookup.stdout.strip()
    return branch or "unknown-branch"


def _collect_commit_summaries(base_branch: str, *, repo_root: Path) -> list[str]:
    primary_range = f"origin/{base_branch}..HEAD"
    primary = _run_command(
        ["git", "log", "--format=%s", "--reverse", primary_range],
        cwd=repo_root,
    )
    lines = [line.strip() for line in primary.stdout.splitlines() if line.strip()]
    if primary.returncode == 0 and lines:
        return lines
    fallback = _run_command(["git", "log", "--format=%s", "-n", "5"], cwd=repo_root)
    fallback_lines = [line.strip() for line in fallback.stdout.splitlines() if line.strip()]
    return fallback_lines


def _render_pr_body(*, pr_title: str, base_branch: str, repo_root: Path) -> str:
    head_branch = _resolve_head_branch(repo_root)
    commit_summaries = _collect_commit_summaries(base_branch, repo_root=repo_root)
    lines: list[str] = [
        pr_title,
        "",
        "## Context",
        f"- Branch: `{head_branch}`",
        f"- Base branch: `{base_branch}`",
        "- Changes:",
    ]
    if commit_summaries:
        for summary in commit_summaries[:8]:
            lines.append(f"  - {summary}")
    else:
        lines.append("  - Commit summary unavailable.")
    lines.extend(
        [
            "",
            "## Testing",
            "- [ ] Describe automated and manual tests run for this branch.",
            "",
        ]
    )
    return "\n".join(lines)


def _write_body_template(
    path: Path,
    *,
    pr_title: str,
    base_branch: str,
    repo_root: Path,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = _render_pr_body(
        pr_title=pr_title,
        base_branch=base_branch,
        repo_root=repo_root,
    )
    path.write_text(body, encoding="utf-8")


def _run_command(
    command: list[str],
    *,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(cwd) if cwd is not None else None,
    )


def _resolve_base_branch(repo_root: Path) -> str:
    base_branch = ""
    gh_lookup = _run_command(
        ["gh", "repo", "view", "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name"],
        cwd=repo_root,
    )
    if gh_lookup.returncode == 0:
        base_branch = gh_lookup.stdout.strip()

    if not base_branch:
        symbolic_ref = _run_command(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
            cwd=repo_root,
        )
        if symbolic_ref.returncode == 0:
            base_branch = symbolic_ref.stdout.strip()
            prefix = "refs/remotes/origin/"
            if base_branch.startswith(prefix):
                base_branch = base_branch[len(prefix) :]

    if not base_branch:
        raise RuntimeError("Unable to determine default base branch")
    return base_branch


def _create_pr(*, title: str, body_file: Path, base_branch: str, repo_root: Path) -> str:
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
        ],
        cwd=repo_root,
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
        repo_root = _resolve_repo_root(run_dir)
        body_file = Path(args.pr_body_file).expanduser()

        base_branch = _resolve_base_branch(repo_root)
        _write_body_template(
            body_file,
            pr_title=args.pr_title,
            base_branch=base_branch,
            repo_root=repo_root,
        )
        pr_url = _create_pr(
            title=args.pr_title,
            body_file=body_file,
            base_branch=base_branch,
            repo_root=repo_root,
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
