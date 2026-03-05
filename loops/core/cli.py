from __future__ import annotations

"""Top-level command-line interface for Loops."""

from dataclasses import replace
from datetime import datetime, timezone
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

import click

from loops.core.inner_loop import reset_run_record, run_inner_loop
from loops.core.outer_loop import (
    InnerLoopCommandConfig,
    OuterLoopRunner,
    OuterLoopState,
    SyncModeInterruptedError,
    _resolve_provider_comment_approval_pattern,
    _resolve_provider_comment_approval_usernames,
    _resolve_provider_review_actor_usernames,
    _select_task_by_url,
    create_run_dir,
    build_default_loop_config_payload,
    build_inner_loop_launcher,
    build_provider,
    load_config,
    read_outer_state,
    _resolve_starting_commit,
    upgrade_config_payload,
    write_outer_state,
)
from loops.state.constants import (
    AGENT_LOG_FILE_NAME,
    INNER_LOOP_RUNS_DIR_NAME,
    LATEST_LOOPS_CONFIG_VERSION,
    OUTER_LOG_FILE_NAME,
    OUTER_STATE_FILE_NAME,
    RUN_LOG_FILE_NAME,
    RUN_RECORD_FILE_NAME,
)
from loops.state.run_record import CodexSession, RunPR, RunRecord, Task, write_run_record
from loops.task_providers.github_projects_v2 import (
    GITHUB_PROJECTS_V2_PROVIDER_ID,
    build_default_provider_config_payload,
)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def main() -> None:
    """Loops CLI wrapper for setup, outer loop, and inner loop."""


def _run_outer_loop(
    *,
    config_path: Path,
    run_once: bool,
    limit: Optional[int],
    force: Optional[bool],
    task_url: Optional[str] = None,
) -> None:
    """Run the configured outer loop."""

    config = load_config(config_path)
    effective_loop_config = config.loop_config
    force_override: Optional[bool]
    if task_url is not None:
        force_override = True
        effective_loop_config = replace(effective_loop_config, sync_mode=True)
    else:
        force_override = force
    if force_override is not None:
        effective_loop_config = replace(effective_loop_config, force=force_override)
    config = replace(config, loop_config=effective_loop_config)
    if config.inner_loop is None:
        config = replace(config, inner_loop=_build_default_inner_loop_config())
    provider = build_provider(config)
    launcher = build_inner_loop_launcher(
        config,
        approval_comment_usernames=_resolve_provider_comment_approval_usernames(
            provider
        ),
        approval_comment_pattern=_resolve_provider_comment_approval_pattern(provider),
        review_actor_usernames=_resolve_provider_review_actor_usernames(provider),
    )
    loops_root = _resolve_loops_root(config_path)
    runner = OuterLoopRunner(
        provider,
        config.loop_config,
        loops_root=loops_root,
        inner_loop_launcher=launcher,
    )
    effective_run_once = run_once or task_url is not None
    try:
        if effective_run_once:
            runner.run_once(limit=limit, forced_task_url=task_url)
        else:
            runner.run_forever(limit=limit)
    except KeyboardInterrupt as exc:
        if isinstance(exc, SyncModeInterruptedError):
            _print_sync_resume_instructions(run_dir=exc.run_dir)
        raise click.Abort() from exc


GITHUB_PR_URL_PATTERN = re.compile(
    r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[0-9]+"
)
GITHUB_ISSUE_URL_PATTERN = re.compile(
    r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/[0-9]+"
)
GITHUB_REPO_URL_PATTERN = re.compile(
    r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/(pull|issues)/[0-9]+$"
)
GITHUB_REMOTE_REPO_PATTERN = re.compile(
    r"(?:github\.com[:/])([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?$"
)


def _run_handoff_command(
    *,
    config_path: Path,
    session_id: Optional[str],
    pr_url: Optional[str],
    task_url: Optional[str],
) -> None:
    """Seed and launch a WAITING_ON_REVIEW run from an existing Codex session."""

    resolved_session_id = _resolve_handoff_session_id(session_id)
    transcript_path = _find_codex_session_transcript(resolved_session_id)

    loops_config = load_config(config_path)
    if loops_config.inner_loop is None:
        loops_config = replace(
            loops_config,
            inner_loop=_build_default_inner_loop_config(),
        )
    provider = build_provider(loops_config)

    repo_slug = _resolve_repo_slug(_resolve_loops_root(config_path).resolve().parent)
    discovered_pr_url: Optional[str] = None
    discovered_task_url: Optional[str] = None
    pr_candidates: tuple[str, ...] = ()
    task_candidates: tuple[str, ...] = ()
    if transcript_path is not None:
        (
            discovered_pr_url,
            discovered_task_url,
            pr_candidates,
            task_candidates,
        ) = _derive_handoff_urls_from_session(
            transcript_path,
            repo_slug=repo_slug,
        )

    resolved_pr_url = _resolve_required_handoff_url(
        label="PR",
        provided=pr_url,
        discovered=discovered_pr_url,
        candidates=pr_candidates,
    )
    preferred_task_repo_slug = _repo_slug_from_github_url(resolved_pr_url) or repo_slug
    if (
        not (task_url or "").strip()
        and discovered_task_url is None
        and task_candidates
    ):
        discovered_task_url, task_candidates = _select_handoff_url_candidate(
            list(task_candidates),
            repo_slug=preferred_task_repo_slug,
            strict_repo=True,
        )
    resolved_task_url = _resolve_required_handoff_url(
        label="tracking task",
        provided=task_url,
        discovered=discovered_task_url,
        candidates=task_candidates,
    )

    polled_tasks = provider.poll(limit=None)
    try:
        task = _select_task_by_url(polled_tasks, resolved_task_url)
    except ValueError:
        task = _build_synthesized_handoff_task(
            resolved_task_url,
            provider_id=loops_config.task_provider_id,
        )

    seeded_pr = _build_seed_handoff_pr(resolved_pr_url)
    loops_root = _resolve_loops_root(config_path)
    run_dir = create_run_dir(task, loops_root)
    now_iso = datetime.now(timezone.utc).isoformat()
    run_record = RunRecord(
        task=task,
        pr=seeded_pr,
        codex_session=CodexSession(id=resolved_session_id),
        needs_user_input=False,
        stream_logs_stdout=loops_config.loop_config.sync_mode,
        checkout_mode=loops_config.loop_config.checkout_mode,
        starting_commit=_resolve_starting_commit(loops_root),
        last_state="WAITING_ON_REVIEW",
        updated_at=now_iso,
    )
    write_run_record(
        run_dir / RUN_RECORD_FILE_NAME,
        run_record,
        auto_approve_enabled=loops_config.loop_config.auto_approve_enabled,
    )
    (run_dir / RUN_LOG_FILE_NAME).touch(exist_ok=True)
    (run_dir / AGENT_LOG_FILE_NAME).touch(exist_ok=True)

    state_path = loops_root / OUTER_STATE_FILE_NAME
    outer_state = read_outer_state(state_path)
    outer_state.record_task(task, now_iso)
    outer_state.initialized = True
    outer_state.updated_at = now_iso
    write_outer_state(state_path, outer_state)

    launcher = build_inner_loop_launcher(
        loops_config,
        approval_comment_usernames=_resolve_provider_comment_approval_usernames(
            provider
        ),
        approval_comment_pattern=_resolve_provider_comment_approval_pattern(provider),
        review_actor_usernames=_resolve_provider_review_actor_usernames(provider),
    )
    launcher(run_dir, task)

    click.echo(f"Handoff started in {run_dir}")
    click.echo(f"Session: {resolved_session_id}")
    click.echo(f"Task: {task.url}")
    click.echo(f"PR: {seeded_pr.url}")


def _resolve_handoff_session_id(session_id: Optional[str]) -> str:
    if session_id is not None:
        normalized = session_id.strip()
        if normalized:
            return normalized

    env_session_id = os.environ.get("CODEX_THREAD_ID", "").strip()
    if env_session_id:
        return env_session_id

    latest = _read_latest_session_id_from_history(
        Path.home() / ".codex" / "history.jsonl"
    )
    if latest is not None:
        return latest

    if _is_interactive_stdin():
        prompted = click.prompt("Codex session id", type=str).strip()
        if prompted:
            return prompted
    raise click.ClickException(
        "Unable to determine Codex session id. Pass `loops handoff <session-id>`."
    )


def _read_latest_session_id_from_history(history_path: Path) -> Optional[str]:
    try:
        lines = history_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for raw_line in reversed(lines):
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        session_id = payload.get("session_id")
        if isinstance(session_id, str) and session_id.strip():
            return session_id.strip()
    return None


def _find_codex_session_transcript(session_id: str) -> Optional[Path]:
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    roots = (
        codex_home / "sessions",
        codex_home / "archived_sessions",
    )
    candidates: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        pattern = f"*{session_id}.jsonl"
        candidates.extend(root.rglob(pattern))
    if not candidates:
        return None
    try:
        return max(candidates, key=lambda path: path.stat().st_mtime)
    except OSError:
        return candidates[-1]


def _derive_handoff_urls_from_session(
    session_path: Path,
    *,
    repo_slug: Optional[str],
) -> tuple[Optional[str], Optional[str], tuple[str, ...], tuple[str, ...]]:
    pr_candidates: list[str] = []
    task_candidates: list[str] = []
    try:
        lines = session_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None, None, (), ()

    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        for text in _iter_handoff_session_texts(payload):
            for match in GITHUB_PR_URL_PATTERN.finditer(text):
                pr_candidates.append(match.group(0))
            for match in GITHUB_ISSUE_URL_PATTERN.finditer(text):
                task_candidates.append(match.group(0))

    selected_pr, filtered_pr_candidates = _select_handoff_url_candidate(
        pr_candidates,
        repo_slug=repo_slug,
    )
    selected_task, filtered_task_candidates = _select_handoff_url_candidate(
        task_candidates,
        repo_slug=repo_slug,
    )
    return selected_pr, selected_task, filtered_pr_candidates, filtered_task_candidates


def _iter_handoff_session_texts(payload: Mapping[str, object]) -> Iterator[str]:
    if payload.get("type") != "response_item":
        return
    nested_payload = payload.get("payload")
    if not isinstance(nested_payload, Mapping):
        return
    if nested_payload.get("type") != "message":
        return
    if nested_payload.get("role") not in {"user", "assistant"}:
        return
    content = nested_payload.get("content")
    if not isinstance(content, list):
        return
    for item in content:
        if not isinstance(item, Mapping):
            continue
        item_type = item.get("type")
        if item_type not in {"input_text", "output_text", "text"}:
            continue
        text = item.get("text")
        if isinstance(text, str):
            stripped = text.strip()
            if stripped:
                yield stripped


def _select_handoff_url_candidate(
    candidates: list[str],
    *,
    repo_slug: Optional[str],
    strict_repo: bool = False,
) -> tuple[Optional[str], tuple[str, ...]]:
    unique_candidates = _dedupe_preserve_order(candidates)
    if not unique_candidates:
        return None, ()
    if repo_slug:
        repo_matches = tuple(
            candidate
            for candidate in unique_candidates
            if _repo_slug_from_github_url(candidate) == repo_slug
        )
        if len(repo_matches) == 1:
            return repo_matches[0], repo_matches
        if len(repo_matches) > 1:
            return None, repo_matches
        if strict_repo:
            return None, ()
    if len(unique_candidates) == 1:
        return unique_candidates[0], unique_candidates
    return None, unique_candidates


def _dedupe_preserve_order(values: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return tuple(deduped)


def _resolve_required_handoff_url(
    *,
    label: str,
    provided: Optional[str],
    discovered: Optional[str],
    candidates: tuple[str, ...],
) -> str:
    normalized_provided = (provided or "").strip()
    if normalized_provided:
        return normalized_provided
    if discovered:
        return discovered

    if _is_interactive_stdin():
        if candidates:
            click.echo(f"Could not determine {label} URL from session context.")
            click.echo("Candidates:")
            for candidate in candidates:
                click.echo(f"- {candidate}")
        prompted = click.prompt(f"Enter {label} URL", type=str).strip()
        if prompted:
            return prompted

    candidate_hint = ""
    if candidates:
        candidate_hint = " Candidates: " + ", ".join(candidates[:5])
    raise click.ClickException(
        f"Unable to determine {label} URL from session and no override was provided."
        f"{candidate_hint}"
    )


def _is_interactive_stdin() -> bool:
    try:
        return click.get_text_stream("stdin").isatty()
    except Exception:
        return False


def _resolve_repo_slug(workspace_root: Path) -> Optional[str]:
    try:
        completed = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=workspace_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    remote_url = completed.stdout.strip()
    if not remote_url:
        return None
    return _repo_slug_from_remote(remote_url)


def _repo_slug_from_remote(remote_url: str) -> Optional[str]:
    match = GITHUB_REMOTE_REPO_PATTERN.search(remote_url.strip())
    if match is None:
        return None
    owner = match.group(1)
    repo = match.group(2)
    return f"{owner}/{repo}".casefold()


def _repo_slug_from_github_url(url: str) -> Optional[str]:
    match = GITHUB_REPO_URL_PATTERN.match(url.strip())
    if match is None:
        return None
    return f"{match.group(1)}/{match.group(2)}".casefold()


def _build_seed_handoff_pr(pr_url: str) -> RunPR:
    normalized_url = pr_url.strip()
    match = GITHUB_PR_URL_PATTERN.search(normalized_url)
    if match is None:
        raise click.ClickException(f"Invalid GitHub PR URL: {pr_url}")
    canonical_url = match.group(0)
    parts = canonical_url.split("/")
    return RunPR(
        url=canonical_url,
        number=int(parts[-1]),
        repo=f"{parts[3]}/{parts[4]}",
        review_status="open",
        ci_status=None,
        ci_last_checked_at=None,
        merged_at=None,
        last_checked_at=None,
        latest_review_submitted_at=None,
        review_addressed_at=None,
    )


def _build_synthesized_handoff_task(task_url: str, *, provider_id: str) -> Task:
    now_iso = datetime.now(timezone.utc).isoformat()
    normalized_url = task_url.strip()
    issue_match = GITHUB_ISSUE_URL_PATTERN.search(normalized_url)
    if issue_match is not None:
        canonical_url = issue_match.group(0)
    else:
        canonical_url = normalized_url
    repo = _repo_slug_from_github_url(canonical_url)
    task_id = canonical_url
    if canonical_url.rstrip("/").split("/")[-1].isdigit():
        task_id = canonical_url.rstrip("/").split("/")[-1]
    return Task(
        provider_id=provider_id,
        id=task_id,
        title=f"Handoff task {task_id}",
        status="handoff",
        url=canonical_url,
        created_at=now_iso,
        updated_at=now_iso,
        repo=repo,
    )


def _run_inner_loop_command(
    *,
    run_dir: Optional[Path],
    prompt_file: Optional[Path],
    reset: bool,
) -> None:
    """Run the inner loop command implementation."""

    resolved_run_dir = _resolve_run_dir_option(run_dir)
    try:
        if reset:
            reset_run_record(resolved_run_dir)
            click.echo(f"Reset run state: {resolved_run_dir / RUN_RECORD_FILE_NAME}")
            return
        run_inner_loop(resolved_run_dir, prompt_file=prompt_file)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc


def _run_init_command(*, loops_root: Path, force: bool) -> None:
    """Run the init command implementation."""

    loops_root = loops_root.resolve()
    loops_root.mkdir(parents=True, exist_ok=True)

    config_path = loops_root / "config.json"
    if config_path.exists() and not force:
        raise click.ClickException(
            f"Config already exists: {config_path} (re-run with --force to overwrite)"
        )

    state_path = loops_root / OUTER_STATE_FILE_NAME
    if not state_path.exists():
        write_outer_state(state_path, OuterLoopState.empty())
    (loops_root / OUTER_LOG_FILE_NAME).touch(exist_ok=True)
    (loops_root / INNER_LOOP_RUNS_DIR_NAME).mkdir(parents=True, exist_ok=True)

    config_path.write_text(
        json.dumps(_build_default_config(), indent=2, sort_keys=True) + "\n"
    )
    click.echo(f"Initialized Loops in {loops_root}")
    click.echo(f"Config: {config_path}")


def _run_doctor_command(*, config_path: Path) -> None:
    """Run the doctor command implementation."""

    resolved_path = config_path.resolve()
    if not resolved_path.exists():
        raise click.ClickException(f"Config does not exist: {resolved_path}")

    try:
        payload = json.loads(resolved_path.read_text())
        upgraded_payload, changed = upgrade_config_payload(payload)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise click.ClickException(str(exc)) from exc

    if changed:
        resolved_path.write_text(
            json.dumps(upgraded_payload, indent=2, sort_keys=True) + "\n"
        )
        click.echo(
            f"Upgraded config to version {LATEST_LOOPS_CONFIG_VERSION}: {resolved_path}"
        )
        return

    click.echo(
        f"Config already up to date (version {LATEST_LOOPS_CONFIG_VERSION}): {resolved_path}"
    )


def _print_sync_resume_instructions(*, run_dir: Path) -> None:
    """Print instructions for resuming an interrupted sync-mode inner loop."""

    click.echo(
        "Sync mode interrupted. Resume this run with:\n"
        f"loops inner-loop --run-dir {shlex.quote(str(run_dir))}"
    )


def _resolve_run_dir_option(run_dir: Optional[Path]) -> Path:
    """Resolve --run-dir with LOOPS_RUN_DIR fallback."""

    if run_dir is not None:
        return run_dir
    env_run_dir = os.environ.get("LOOPS_RUN_DIR")
    if env_run_dir:
        return Path(env_run_dir)
    raise click.ClickException("LOOPS_RUN_DIR is required (or pass --run-dir)")


def _build_default_config() -> dict[str, Any]:
    """Build a default Loops config payload for `loops init`."""

    return {
        "version": LATEST_LOOPS_CONFIG_VERSION,
        "task_provider_id": GITHUB_PROJECTS_V2_PROVIDER_ID,
        "task_provider_config": build_default_provider_config_payload(),
        "loop_config": build_default_loop_config_payload(),
        "inner_loop": _build_default_inner_loop_payload(),
    }


def _build_default_inner_loop_config() -> InnerLoopCommandConfig:
    """Build the default runtime fallback InnerLoopCommandConfig."""

    return InnerLoopCommandConfig(
        command=[sys.executable, "-m", "loops", "inner-loop"],
        append_task_url=False,
    )


def _build_default_inner_loop_payload() -> dict[str, Any]:
    """Build the default JSON payload for `inner_loop` config."""

    defaults = _build_default_inner_loop_config()
    return {
        "command": defaults.command,
        "append_task_url": defaults.append_task_url,
    }


def _resolve_loops_root(config_path: Path) -> Path:
    """Resolve the loops root directory based on the config path."""

    resolved = config_path.resolve()
    if resolved.parent.name == ".loops":
        return resolved.parent
    return resolved.parent / ".loops"

# Import subcommands at the end to avoid circular imports: command modules
# call helpers from this module.
from loops.commands.clean import clean_command as clean_command
from loops.commands.doctor import doctor_command as doctor_command
from loops.commands.handoff import handoff_command as handoff_command
from loops.commands.init import init_command as init_command
from loops.commands.inner_loop import inner_loop_command as inner_loop_command
from loops.commands.run import run_command as run_command

main.add_command(run_command)
main.add_command(inner_loop_command)
main.add_command(init_command)
main.add_command(doctor_command)
main.add_command(clean_command)
main.add_command(handoff_command)


if __name__ == "__main__":
    main(prog_name="loops")
