from __future__ import annotations

"""Inner loop runner for executing Codex with the unified prompt."""

import argparse
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

from loops.approval_config import (
    DEFAULT_APPROVAL_COMMENT_PATTERN,
    InnerLoopApprovalConfig,
    read_inner_loop_approval_config,
)
from loops.logging_utils import append_log
from loops.run_record import (
    CodexSession,
    RunPR,
    RunRecord,
    Task,
    derive_run_state,
    read_run_record,
    write_run_record,
)
from loops.state_signal import SIGNAL_QUEUE_FILE

PROMPT_TEMPLATE = (
    "Use dev.do to implement the task, open a PR, wait for review, address feedback, "
    "and cleanup when approved.\n"
    'If needing input from user, use "$needs_input" skill to request user input.\n'
    "The current inner-loop state is passed via a trailing <state>...</state> tag; "
    "initial state is <state>START</state>.\n"
    "Do not merge until the state is exactly <state>PR_APPROVED</state>.\n"
    "Task: {task}\n"
)
PROMPT_STATE_START = "START"
PROMPT_STATE_WAITING_ON_REVIEW = "WAITING_ON_REVIEW"
PROMPT_STATE_PR_APPROVED = "PR_APPROVED"
SIGNAL_OFFSET_FILE = "state_signals.offset"
DEFAULT_MAX_ITERATIONS = 200
DEFAULT_REVIEW_POLL_SECONDS = 5.0
DEFAULT_MAX_REVIEW_POLL_SECONDS = 60.0
DEFAULT_MAX_IDLE_POLLS = 20
GITHUB_PR_PATTERN = re.compile(
    r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/([0-9]+)"
)

SESSION_ID_PATTERN = re.compile(r"session[_\s-]*id\s*[:=]\s*([\w-]+)", re.IGNORECASE)
UUID_PATTERN = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
UserHandoffHandler = Callable[[dict[str, Any]], str]
PRStatusFetcher = Callable[[RunPR], RunPR]
SleepFn = Callable[[float], None]
GH_PR_VIEW_JSON_FIELDS = "reviewDecision,mergedAt,url,number,latestReviews,comments"


@dataclass(frozen=True)
class CommentApprovalSettings:
    allowed_usernames: tuple[str, ...]
    pattern_text: str
    approval_regex: re.Pattern[str]
    used_default_pattern: bool = False
    config_load_error: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.allowed_usernames)


def reset_run_record(run_dir: Path) -> RunRecord:
    """Reset run.json orchestration state while preserving durable identifiers."""

    resolved_run_dir = run_dir.resolve()
    run_json_path = resolved_run_dir / "run.json"
    run_log = resolved_run_dir / "run.log"

    existing_record: Optional[RunRecord] = None
    task: Optional[Task] = None
    if run_json_path.exists():
        try:
            existing_record = read_run_record(run_json_path)
            task = existing_record.task
        except Exception as exc:
            append_log(
                run_log,
                f"[loops] warning: failed to read existing run.json during reset: {exc}",
            )

    if task is None:
        task = _build_reset_task_from_env(resolved_run_dir)

    reset_record = RunRecord(
        task=task,
        pr=_build_reset_pr(existing_record.pr if existing_record is not None else None),
        codex_session=None,
        needs_user_input=False,
        needs_user_input_payload=None,
        last_state="RUNNING",
        updated_at="",
    )
    written = write_run_record(run_json_path, reset_record)
    append_log(
        run_log,
        (
            "[loops] run.json reset to initial state "
            f"(task_id={written.task.id}, task_url={written.task.url}, "
            f"pr_url={written.pr.url if written.pr is not None else 'none'})"
        ),
    )
    return written


def run_inner_loop(
    run_dir: Path,
    *,
    prompt_file: Optional[Path] = None,
    user_handoff_handler: Optional[UserHandoffHandler] = None,
    pr_status_fetcher: Optional[PRStatusFetcher] = None,
    sleep_fn: SleepFn = time.sleep,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    initial_poll_seconds: float = DEFAULT_REVIEW_POLL_SECONDS,
    max_poll_seconds: float = DEFAULT_MAX_REVIEW_POLL_SECONDS,
    max_idle_polls: int = DEFAULT_MAX_IDLE_POLLS,
) -> RunRecord:
    """Run the inner loop for a single task directory."""

    if max_iterations <= 0:
        raise ValueError("max_iterations must be positive")

    run_dir = run_dir.resolve()
    run_json_path = run_dir / "run.json"
    run_log = run_dir / "run.log"
    agent_log = run_dir / "agent.log"
    base_prompt = _load_prompt_file(prompt_file)
    command = _resolve_codex_command()
    comment_approval = _load_comment_approval_settings(run_dir)
    if comment_approval.config_load_error is not None:
        append_log(
            run_log,
            (
                "[loops] failed to load run approval config; using defaults: "
                f"{comment_approval.config_load_error}"
            ),
        )
    if comment_approval.used_default_pattern:
        append_log(
            run_log,
            (
                "[loops] invalid approval comment pattern in run approval config; "
                "falling back to default"
            ),
        )
    if pr_status_fetcher is None:
        def _default_pr_status_fetcher(pr: RunPR) -> RunPR:
            updated_pr, approved_by_comment, approved_by = _fetch_pr_status_with_gh_with_context(
                pr,
                comment_approval=comment_approval,
                log_message=lambda message: append_log(run_log, message),
            )
            if approved_by_comment:
                append_log(
                    run_log,
                    (
                        "[loops] treating PR as approved via allowlisted approval "
                        f"comment by {approved_by}"
                    ),
                )
            return updated_pr

        pr_status_fetcher = _default_pr_status_fetcher
    using_default_handoff_handler = user_handoff_handler is None
    user_handoff_handler = user_handoff_handler or _default_user_handoff_handler
    non_interactive_default_handoff = (
        using_default_handoff_handler and not _has_interactive_stdin()
    )
    backoff_seconds = initial_poll_seconds
    idle_polls = 0
    next_user_response: Optional[str] = None
    cleanup_executed_for_pr: Optional[str] = None

    for iteration in range(1, max_iterations + 1):
        run_record = read_run_record(run_json_path)
        run_record = _apply_pending_signals(run_dir, run_record)
        state = derive_run_state(run_record.pr, run_record.needs_user_input)
        _log_iteration_enter(
            run_log,
            iteration=iteration,
            state=state,
            run_record=run_record,
            backoff_seconds=backoff_seconds,
            idle_polls=idle_polls,
        )

        if state == "DONE":
            append_log(run_log, "[loops] run state DONE; exiting inner loop")
            _log_iteration_exit(
                run_log,
                iteration=iteration,
                next_state=state,
                run_record=run_record,
                action="done_exit",
                backoff_seconds=backoff_seconds,
                idle_polls=idle_polls,
            )
            return run_record

        if state == "NEEDS_INPUT":
            response = _handle_needs_input(
                run_record,
                user_handoff_handler,
                run_log,
            )
            if response is None:
                if non_interactive_default_handoff:
                    append_log(
                        run_log,
                        "[loops] non-interactive mode; exiting while waiting for user input",
                    )
                    terminal_record = read_run_record(run_json_path)
                    _log_iteration_exit(
                        run_log,
                        iteration=iteration,
                        next_state=derive_run_state(
                            terminal_record.pr,
                            terminal_record.needs_user_input,
                        ),
                        run_record=terminal_record,
                        action="needs_input_non_interactive_exit",
                        backoff_seconds=backoff_seconds,
                        idle_polls=idle_polls,
                    )
                    return terminal_record
                sleep_fn(min(backoff_seconds, max_poll_seconds))
                backoff_seconds = min(backoff_seconds * 2, max_poll_seconds)
                _log_iteration_exit(
                    run_log,
                    iteration=iteration,
                    next_state=derive_run_state(
                        run_record.pr,
                        run_record.needs_user_input,
                    ),
                    run_record=run_record,
                    action="needs_input_waiting",
                    backoff_seconds=backoff_seconds,
                    idle_polls=idle_polls,
                )
                continue

            next_user_response = response
            run_record = write_run_record(
                run_json_path,
                replace(
                    run_record,
                    needs_user_input=False,
                    needs_user_input_payload=None,
                ),
            )
            backoff_seconds = initial_poll_seconds
            idle_polls = 0
            _log_iteration_exit(
                run_log,
                iteration=iteration,
                next_state=derive_run_state(
                    run_record.pr,
                    run_record.needs_user_input,
                ),
                run_record=run_record,
                action="needs_input_cleared",
                backoff_seconds=backoff_seconds,
                idle_polls=idle_polls,
            )
            continue

        if state == "RUNNING":
            run_record = _run_codex_turn(
                run_json_path=run_json_path,
                run_log=run_log,
                agent_log=agent_log,
                run_record=run_record,
                command=command,
                base_prompt=base_prompt,
                user_response=next_user_response,
                review_feedback=False,
            )
            next_user_response = None
            cleanup_executed_for_pr = None
            backoff_seconds = initial_poll_seconds
            idle_polls = 0
            _log_iteration_exit(
                run_log,
                iteration=iteration,
                next_state=derive_run_state(
                    run_record.pr,
                    run_record.needs_user_input,
                ),
                run_record=run_record,
                action="codex_turn",
                backoff_seconds=backoff_seconds,
                idle_polls=idle_polls,
            )
            continue

        if state == "WAITING_ON_REVIEW":
            if run_record.pr is None:
                run_record = _force_needs_input(
                    run_json_path,
                    run_record,
                    message="Run is waiting on review but no PR metadata exists.",
                )
                _log_iteration_exit(
                    run_log,
                    iteration=iteration,
                    next_state=derive_run_state(
                        run_record.pr,
                        run_record.needs_user_input,
                    ),
                    run_record=run_record,
                    action="review_missing_pr",
                    backoff_seconds=backoff_seconds,
                    idle_polls=idle_polls,
                )
                continue
            try:
                updated_pr = pr_status_fetcher(run_record.pr)
            except Exception as exc:
                append_log(run_log, f"[loops] failed to poll PR status: {exc}")
                idle_polls += 1
                if idle_polls >= max_idle_polls:
                    run_record = _force_needs_input(
                        run_json_path,
                        run_record,
                        message=(
                            "PR polling has been idle for too long. "
                            "Please check review status manually."
                        ),
                    )
                    idle_polls = 0
                sleep_fn(min(backoff_seconds, max_poll_seconds))
                backoff_seconds = min(backoff_seconds * 2, max_poll_seconds)
                _log_iteration_exit(
                    run_log,
                    iteration=iteration,
                    next_state=derive_run_state(
                        run_record.pr,
                        run_record.needs_user_input,
                    ),
                    run_record=run_record,
                    action="review_poll_error",
                    backoff_seconds=backoff_seconds,
                    idle_polls=idle_polls,
                )
                continue

            run_record = write_run_record(
                run_json_path,
                replace(run_record, pr=updated_pr),
            )
            if (
                run_record.pr is not None
                and run_record.pr.review_status == "changes_requested"
                and _is_new_review(run_record.pr)
            ):
                append_log(run_log, "[loops] review changes requested; resuming codex")
                run_record = _run_codex_turn(
                    run_json_path=run_json_path,
                    run_log=run_log,
                    agent_log=agent_log,
                    run_record=run_record,
                    command=command,
                    base_prompt=base_prompt,
                    user_response=next_user_response,
                    review_feedback=True,
                )
                next_user_response = None
                cleanup_executed_for_pr = None
                backoff_seconds = initial_poll_seconds
                idle_polls = 0
                _log_iteration_exit(
                    run_log,
                    iteration=iteration,
                    next_state=derive_run_state(
                        run_record.pr,
                        run_record.needs_user_input,
                    ),
                    run_record=run_record,
                    action="review_feedback_codex_turn",
                    backoff_seconds=backoff_seconds,
                    idle_polls=idle_polls,
                )
                continue
            next_state = derive_run_state(run_record.pr, run_record.needs_user_input)
            if next_state == "WAITING_ON_REVIEW":
                idle_polls += 1
                if idle_polls >= max_idle_polls:
                    run_record = _force_needs_input(
                        run_json_path,
                        run_record,
                        message=(
                            "PR has not changed after repeated polls. "
                            "Please provide manual guidance."
                        ),
                    )
                    idle_polls = 0
            else:
                idle_polls = 0
                backoff_seconds = initial_poll_seconds
            sleep_fn(min(backoff_seconds, max_poll_seconds))
            backoff_seconds = min(backoff_seconds * 2, max_poll_seconds)
            _log_iteration_exit(
                run_log,
                iteration=iteration,
                next_state=next_state,
                run_record=run_record,
                action="review_poll",
                backoff_seconds=backoff_seconds,
                idle_polls=idle_polls,
            )
            continue

        if state == "PR_APPROVED":
            if run_record.pr is None:
                run_record = _force_needs_input(
                    run_json_path,
                    run_record,
                    message="Run is PR_APPROVED but no PR metadata exists.",
                )
                _log_iteration_exit(
                    run_log,
                    iteration=iteration,
                    next_state=derive_run_state(
                        run_record.pr,
                        run_record.needs_user_input,
                    ),
                    run_record=run_record,
                    action="approved_missing_pr",
                    backoff_seconds=backoff_seconds,
                    idle_polls=idle_polls,
                )
                continue

            # Run cleanup once for a given PR URL, then only poll until merged.
            if cleanup_executed_for_pr != run_record.pr.url:
                cleanup_prompt = _build_cleanup_prompt(run_record.task.url, base_prompt)
                output, exit_code = _run_codex(command, cleanup_prompt, agent_log)
                append_log(run_log, output)
                if exit_code != 0:
                    run_record = _force_needs_input(
                        run_json_path,
                        run_record,
                        message="Cleanup failed after PR approval. Please advise.",
                        context={"exit_code": exit_code},
                    )
                    _log_iteration_exit(
                        run_log,
                        iteration=iteration,
                        next_state=derive_run_state(
                            run_record.pr,
                            run_record.needs_user_input,
                        ),
                        run_record=run_record,
                        action="cleanup_failed",
                        backoff_seconds=backoff_seconds,
                        idle_polls=idle_polls,
                    )
                    continue
                cleanup_executed_for_pr = run_record.pr.url

            try:
                updated_pr = pr_status_fetcher(run_record.pr)
            except Exception as exc:
                append_log(run_log, f"[loops] failed to poll merge status: {exc}")
                sleep_fn(min(backoff_seconds, max_poll_seconds))
                backoff_seconds = min(backoff_seconds * 2, max_poll_seconds)
                _log_iteration_exit(
                    run_log,
                    iteration=iteration,
                    next_state=derive_run_state(
                        run_record.pr,
                        run_record.needs_user_input,
                    ),
                    run_record=run_record,
                    action="merge_poll_error",
                    backoff_seconds=backoff_seconds,
                    idle_polls=idle_polls,
                )
                continue

            run_record = write_run_record(
                run_json_path,
                replace(run_record, pr=updated_pr),
            )
            sleep_fn(min(backoff_seconds, max_poll_seconds))
            backoff_seconds = min(backoff_seconds * 2, max_poll_seconds)
            _log_iteration_exit(
                run_log,
                iteration=iteration,
                next_state=derive_run_state(run_record.pr, run_record.needs_user_input),
                run_record=run_record,
                action="approved_poll",
                backoff_seconds=backoff_seconds,
                idle_polls=idle_polls,
            )
            continue

    final_record = read_run_record(run_json_path)
    final_record = _force_needs_input(
        run_json_path,
        final_record,
        message=(
            "Inner loop reached max iterations without DONE. "
            "Please provide guidance."
        ),
    )
    append_log(
        run_log,
        (
            "[loops] iteration limit reached; forcing NEEDS_INPUT "
            f"(max_iterations={max_iterations})"
        ),
    )
    return final_record


def _build_reset_task_from_env(run_dir: Path) -> Task:
    now_iso = datetime.now(timezone.utc).isoformat()
    default_label = run_dir.name or "unknown-task"
    return Task(
        provider_id=os.environ.get("LOOPS_TASK_PROVIDER", "unknown"),
        id=os.environ.get("LOOPS_TASK_ID", default_label),
        title=os.environ.get("LOOPS_TASK_TITLE", default_label),
        status="ready",
        url=os.environ.get("LOOPS_TASK_URL", "unknown"),
        created_at=now_iso,
        updated_at=now_iso,
        repo=None,
    )


def _build_reset_pr(existing_pr: Optional[RunPR]) -> Optional[RunPR]:
    if existing_pr is None:
        return None
    return RunPR(
        url=existing_pr.url,
        number=existing_pr.number,
        repo=existing_pr.repo,
        review_status="open",
        merged_at=None,
        last_checked_at=None,
        latest_review_submitted_at=None,
        review_addressed_at=None,
    )


def _log_iteration_enter(
    run_log: Path,
    *,
    iteration: int,
    state: str,
    run_record: RunRecord,
    backoff_seconds: float,
    idle_polls: int,
) -> None:
    append_log(
        run_log,
        (
            f"[loops] iteration {iteration} enter: state={state} "
            f"{_format_run_record_log_details(run_record)} "
            f"backoff_seconds={backoff_seconds:.1f} idle_polls={idle_polls}"
        ),
    )


def _log_iteration_exit(
    run_log: Path,
    *,
    iteration: int,
    next_state: str,
    run_record: RunRecord,
    action: str,
    backoff_seconds: float,
    idle_polls: int,
) -> None:
    append_log(
        run_log,
        (
            f"[loops] iteration {iteration} exit: next_state={next_state} action={action} "
            f"{_format_run_record_log_details(run_record)} "
            f"backoff_seconds={backoff_seconds:.1f} idle_polls={idle_polls}"
        ),
    )


def _format_run_record_log_details(run_record: RunRecord) -> str:
    pr = run_record.pr
    if pr is None:
        pr_summary = "pr_status=none pr_number=- pr_merged=no"
    else:
        review_status = pr.review_status or "unknown"
        pr_number = pr.number if pr.number is not None else "-"
        pr_merged = "yes" if pr.merged_at else "no"
        pr_summary = (
            f"pr_status={review_status} pr_number={pr_number} pr_merged={pr_merged}"
        )
    return f"needs_user_input={run_record.needs_user_input} {pr_summary}"


def _resolve_codex_command() -> list[str]:
    raw_command = os.environ.get("CODEX_CMD", "codex exec --yolo")
    command = shlex.split(raw_command)
    if not command:
        raise ValueError("CODEX_CMD cannot be empty")
    return command


def _load_prompt_file(prompt_file: Optional[Path]) -> Optional[str]:
    if prompt_file is None:
        prompt_path = os.environ.get("LOOPS_PROMPT_FILE") or os.environ.get(
            "CODEX_PROMPT_FILE"
        )
        if prompt_path:
            prompt_file = Path(prompt_path)
    if prompt_file is None:
        return None
    if not prompt_file.is_file():
        raise FileNotFoundError(f"Prompt file not found: {prompt_file}")
    return prompt_file.read_text()


def _build_prompt(
    task_url: str,
    base_prompt: Optional[str],
    *,
    user_response: Optional[str] = None,
    state: Optional[str] = PROMPT_STATE_START,
) -> str:
    prompt = PROMPT_TEMPLATE.format(task=task_url)
    if user_response is not None and user_response.strip():
        prompt += f"\nUser input:\n{user_response.strip()}\n"
    if base_prompt:
        trimmed = base_prompt.rstrip()
        prompt = f"{trimmed}\n\n{prompt}"
    if state is not None:
        prompt = _append_state_tag(prompt, state)
    return prompt


def _append_state_tag(prompt: str, state: str) -> str:
    state_value = state.strip().upper()
    return f"{prompt.rstrip()}\n<state>{state_value}</state>\n"


def _build_cleanup_prompt(task_url: str, base_prompt: Optional[str]) -> str:
    prompt = _build_prompt(task_url, base_prompt, state=None)
    prompt += "\nPR is approved. Run cleanup now and report completion.\n"
    return _append_state_tag(prompt, PROMPT_STATE_PR_APPROVED)


def _build_review_feedback_prompt(
    task_url: str,
    base_prompt: Optional[str],
    pr_url: str,
    *,
    user_response: Optional[str] = None,
) -> str:
    prompt = _build_prompt(
        task_url,
        base_prompt,
        user_response=user_response,
        state=None,
    )
    prompt += (
        f"\nPR {pr_url} has changes requested. Address review feedback, update the PR, "
        "and summarize what changed.\n"
    )
    return _append_state_tag(prompt, PROMPT_STATE_WAITING_ON_REVIEW)


def _run_codex(command: list[str], prompt: str, agent_log: Path) -> tuple[str, int]:
    agent_log.parent.mkdir(parents=True, exist_ok=True)
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=os.environ.copy(),
            bufsize=1,
        )
        if process.stdin is not None:
            process.stdin.write(prompt)
            process.stdin.close()

        lines: list[str] = []
        with agent_log.open("a", encoding="utf-8") as handle:
            if process.stdout is not None:
                for line in process.stdout:
                    lines.append(line)
                    handle.write(line)
                    handle.flush()

        exit_code = process.wait()
        output = "".join(lines)
        return output, exit_code
    except Exception as exc:  # pragma: no cover - defensive logging
        message = f"[loops] codex invocation failed: {exc}"
        return message, 1


def _extract_session_id(output: str) -> Optional[str]:
    for line in output.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            if "session_id" in payload:
                return str(payload["session_id"])
            if "session" in payload:
                return str(payload["session"])
    match = SESSION_ID_PATTERN.search(output)
    if match:
        return match.group(1)
    match = UUID_PATTERN.search(output)
    if match:
        return match.group(0)
    return None


def _run_codex_turn(
    *,
    run_json_path: Path,
    run_log: Path,
    agent_log: Path,
    run_record: RunRecord,
    command: list[str],
    base_prompt: Optional[str],
    user_response: Optional[str] = None,
    review_feedback: bool,
) -> RunRecord:
    if review_feedback and run_record.pr is not None:
        prompt = _build_review_feedback_prompt(
            run_record.task.url,
            base_prompt,
            run_record.pr.url,
            user_response=user_response,
        )
    else:
        prompt = _build_prompt(
            run_record.task.url,
            base_prompt,
            user_response=user_response,
        )

    output, exit_code = _run_codex(command, prompt, agent_log)
    append_log(run_log, output)

    session_id = _extract_session_id(output)
    codex_session = run_record.codex_session
    if session_id is not None:
        codex_session = CodexSession(id=session_id, last_prompt=prompt)
    elif exit_code == 0:
        append_log(run_log, "[loops] warning: no session id detected in codex output")

    discovered_pr = _extract_pr_from_output(output)
    pr = _merge_pr_records(run_record.pr, discovered_pr)
    needs_user_input = exit_code != 0
    needs_user_input_payload = run_record.needs_user_input_payload
    if exit_code != 0:
        append_log(run_log, f"[loops] codex exit code {exit_code}")
        needs_user_input_payload = {
            "message": "Codex exited with a non-zero status. Provide guidance.",
            "context": {"exit_code": exit_code},
        }
    elif pr is None:
        needs_user_input = True
        needs_user_input_payload = {
            "message": (
                "Codex run completed without opening a PR or requesting input. "
                "What should Loops do next?"
            )
        }
        append_log(
            run_log,
            "[loops] no PR detected after codex run; requesting user input",
        )
    elif review_feedback:
        # Record which review event we addressed so we don't re-invoke
        # for the same review. Keep review_status as-is (GitHub is authoritative).
        pr = replace(pr, review_addressed_at=pr.latest_review_submitted_at)

    return write_run_record(
        run_json_path,
        replace(
            run_record,
            pr=pr,
            codex_session=codex_session,
            needs_user_input=needs_user_input,
            needs_user_input_payload=needs_user_input_payload,
        ),
    )


def _extract_pr_from_output(output: str) -> Optional[RunPR]:
    for line in output.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            for key in ("pr_url", "pull_request_url", "url"):
                value = payload.get(key)
                if isinstance(value, str):
                    pr = _run_pr_from_url(value)
                    if pr is not None:
                        return pr
    match = GITHUB_PR_PATTERN.search(output)
    if not match:
        return None
    return _run_pr_from_url(match.group(0))


def _run_pr_from_url(url: str) -> Optional[RunPR]:
    match = GITHUB_PR_PATTERN.search(url)
    if not match:
        return None
    owner = match.group(1)
    repo_name = match.group(2)
    number = int(match.group(3))
    return RunPR(
        url=match.group(0),
        number=number,
        repo=f"{owner}/{repo_name}",
        review_status="open",
    )


def _merge_pr_records(existing: Optional[RunPR], discovered: Optional[RunPR]) -> Optional[RunPR]:
    if discovered is None:
        return existing
    if existing is None:
        return discovered
    if existing.url != discovered.url:
        return discovered
    return replace(
        existing,
        number=existing.number or discovered.number,
        repo=existing.repo or discovered.repo,
        review_status=existing.review_status or discovered.review_status,
        latest_review_submitted_at=existing.latest_review_submitted_at,
        review_addressed_at=existing.review_addressed_at,
    )


def _is_new_review(pr: RunPR) -> bool:
    """Return True if the PR has a review event not yet addressed by Codex."""
    if pr.latest_review_submitted_at is None:
        return True
    if pr.review_addressed_at is None:
        return True
    return pr.latest_review_submitted_at > pr.review_addressed_at


def _extract_latest_review_submitted_at(
    payload: dict[str, Any],
    review_decision: str,
) -> Optional[str]:
    """Return submittedAt of the latest review matching the decision."""
    latest_reviews = payload.get("latestReviews")
    if not isinstance(latest_reviews, list) or not latest_reviews:
        return None
    target_state = review_decision.upper() if review_decision else ""
    best_timestamp: Optional[str] = None
    for review in latest_reviews:
        if not isinstance(review, dict):
            continue
        if str(review.get("state", "")).upper() != target_state:
            continue
        submitted_at = review.get("submittedAt")
        if isinstance(submitted_at, str):
            if best_timestamp is None or submitted_at > best_timestamp:
                best_timestamp = submitted_at
    return best_timestamp


def _review_status_from_decision(decision: Any) -> str:
    normalized = str(decision or "").upper()
    if normalized == "APPROVED":
        return "approved"
    if normalized == "CHANGES_REQUESTED":
        return "changes_requested"
    return "open"


def _load_comment_approval_settings(run_dir: Path) -> CommentApprovalSettings:
    config_load_error: str | None = None
    try:
        config = read_inner_loop_approval_config(run_dir)
    except Exception as exc:
        config_load_error = str(exc)
        config = InnerLoopApprovalConfig()
    allowed_usernames = config.approval_comment_usernames
    pattern_text = config.approval_comment_pattern or DEFAULT_APPROVAL_COMMENT_PATTERN
    used_default_pattern = False
    try:
        approval_regex = re.compile(pattern_text, re.IGNORECASE)
    except re.error:
        approval_regex = re.compile(DEFAULT_APPROVAL_COMMENT_PATTERN, re.IGNORECASE)
        pattern_text = DEFAULT_APPROVAL_COMMENT_PATTERN
        used_default_pattern = True
    return CommentApprovalSettings(
        allowed_usernames=allowed_usernames,
        pattern_text=pattern_text,
        approval_regex=approval_regex,
        used_default_pattern=used_default_pattern,
        config_load_error=config_load_error,
    )


def _extract_latest_allowlisted_approval_comment(
    payload: dict[str, Any],
    comment_approval: CommentApprovalSettings,
) -> tuple[str, str] | None:
    if not comment_approval.enabled:
        return None
    comments = payload.get("comments")
    if not isinstance(comments, list) or not comments:
        return None
    latest: tuple[str, str] | None = None
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        author_payload = comment.get("author")
        author_login = (
            author_payload.get("login")
            if isinstance(author_payload, dict)
            else None
        )
        if not isinstance(author_login, str):
            continue
        if author_login.casefold() not in comment_approval.allowed_usernames:
            continue
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        if comment_approval.approval_regex.search(body) is None:
            continue
        created_at = comment.get("createdAt")
        updated_at = comment.get("updatedAt")
        comment_timestamp = updated_at if isinstance(updated_at, str) else created_at
        if not isinstance(comment_timestamp, str):
            continue
        if latest is None or comment_timestamp > latest[0]:
            latest = (comment_timestamp, author_login)
    return latest


def _fetch_pr_status_with_gh_with_context(
    pr: RunPR,
    *,
    comment_approval: CommentApprovalSettings,
    log_message: Optional[Callable[[str], None]] = None,
) -> tuple[RunPR, bool, str]:
    def _log(message: str) -> None:
        if log_message is None:
            return
        log_message(message)

    _log(
        (
            "[loops] polling PR status via gh: "
            f"pr_url={pr.url} "
            f"comment_approval_enabled={'yes' if comment_approval.enabled else 'no'} "
            f"allowlisted_usernames={len(comment_approval.allowed_usernames)}"
        )
    )
    result = subprocess.run(
        [
            "gh",
            "pr",
            "view",
            pr.url,
            "--json",
            GH_PR_VIEW_JSON_FIELDS,
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=os.environ.copy(),
    )
    if result.returncode != 0:
        _log(
            (
                "[loops] gh pr view failed: "
                f"pr_url={pr.url} "
                f"returncode={result.returncode} "
                f"stderr={result.stderr.strip() or '-'}"
            )
        )
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        snippet = result.stdout.strip().replace("\n", " ")[:200]
        _log(
            (
                "[loops] gh pr view returned invalid JSON: "
                f"pr_url={pr.url} snippet={snippet or '-'}"
            )
        )
        raise

    pr_url = str(payload.get("url") or pr.url)
    parsed_url_pr = _run_pr_from_url(pr_url)

    repo = pr.repo
    if parsed_url_pr is not None and parsed_url_pr.repo is not None:
        repo = parsed_url_pr.repo

    number = payload.get("number")
    if isinstance(number, int):
        parsed_number = number
    elif isinstance(number, str) and number.isdigit():
        parsed_number = int(number)
    elif pr.number is not None:
        parsed_number = pr.number
    elif parsed_url_pr is not None:
        parsed_number = parsed_url_pr.number
    else:
        parsed_number = None
    merged_at = payload.get("mergedAt")
    merged_at_str = str(merged_at) if merged_at is not None else None
    review_decision_raw = payload.get("reviewDecision")
    review_status = _review_status_from_decision(review_decision_raw)
    latest_review_submitted_at = _extract_latest_review_submitted_at(
        payload, str(review_decision_raw or "")
    )
    approved_by_comment = False
    approved_by = ""
    if review_status != "approved":
        latest_changes_requested_at = _extract_latest_review_submitted_at(
            payload,
            "CHANGES_REQUESTED",
        )
        latest_approval_comment = _extract_latest_allowlisted_approval_comment(
            payload,
            comment_approval,
        )
        if latest_approval_comment is not None:
            comment_timestamp, approval_author = latest_approval_comment
            if (
                latest_changes_requested_at is None
                or comment_timestamp > latest_changes_requested_at
            ):
                review_status = "approved"
                approved_by_comment = True
                approved_by = approval_author
                latest_review_submitted_at = comment_timestamp
            else:
                _log(
                    (
                        "[loops] ignoring allowlisted approval comment because a newer "
                        "changes_requested review exists: "
                        f"pr_url={pr.url} "
                        f"approval_comment_at={comment_timestamp} "
                        f"latest_changes_requested_at={latest_changes_requested_at}"
                    )
                )

    updated_pr = RunPR(
        url=pr_url,
        number=parsed_number,
        repo=repo,
        review_status=review_status,
        merged_at=merged_at_str,
        last_checked_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        latest_review_submitted_at=latest_review_submitted_at,
        review_addressed_at=pr.review_addressed_at,
    )
    _log(
        (
            "[loops] PR status poll result: "
            f"pr_url={updated_pr.url} "
            f"review_decision={str(review_decision_raw or '').lower() or 'none'} "
            f"review_status={updated_pr.review_status or 'unknown'} "
            f"merged={'yes' if updated_pr.merged_at else 'no'} "
            f"approved_by_comment={'yes' if approved_by_comment else 'no'} "
            f"approved_by={approved_by or '-'} "
            f"latest_review_submitted_at={updated_pr.latest_review_submitted_at or '-'}"
        )
    )
    return updated_pr, approved_by_comment, approved_by


def _default_user_handoff_handler(payload: dict[str, Any]) -> str:
    message = str(payload.get("message") or "Input required to continue:")
    context = payload.get("context")
    print(f"[loops] {message}")
    if context:
        print(f"[loops] context: {json.dumps(context, ensure_ascii=True, sort_keys=True)}")
    print("[loops] response: ", end="", flush=True)
    return input().strip()


def _has_interactive_stdin() -> bool:
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


def _handle_needs_input(
    run_record: RunRecord,
    handler: UserHandoffHandler,
    run_log: Path,
) -> Optional[str]:
    payload = run_record.needs_user_input_payload or {
        "message": "Input required to continue.",
    }
    try:
        response = handler(payload)
    except EOFError:
        append_log(run_log, "[loops] unable to read user input from stdin")
        return None
    except Exception as exc:
        append_log(run_log, f"[loops] user handoff handler failed: {exc}")
        return None
    normalized = response.strip()
    if not normalized:
        append_log(run_log, "[loops] empty user response received")
        return None
    append_log(run_log, "[loops] user input received")
    return normalized


def _force_needs_input(
    run_json_path: Path,
    run_record: RunRecord,
    *,
    message: str,
    context: Optional[dict[str, Any]] = None,
) -> RunRecord:
    payload: dict[str, Any] = {"message": message}
    if context:
        payload["context"] = context
    return write_run_record(
        run_json_path,
        replace(
            run_record,
            needs_user_input=True,
            needs_user_input_payload=payload,
        ),
    )


def _read_signal_offset(offset_path: Path) -> int:
    if not offset_path.exists():
        return 0
    raw = offset_path.read_text().strip()
    if not raw:
        return 0
    try:
        value = int(raw)
    except ValueError:
        return 0
    return max(value, 0)


def _read_pending_signals(run_dir: Path) -> tuple[list[dict[str, Any]], int]:
    queue_path = run_dir / SIGNAL_QUEUE_FILE
    offset_path = run_dir / SIGNAL_OFFSET_FILE
    if not queue_path.exists():
        return [], 0

    previous_offset = _read_signal_offset(offset_path)
    file_size = queue_path.stat().st_size
    if previous_offset > file_size:
        previous_offset = 0
    with queue_path.open("r", encoding="utf-8") as handle:
        handle.seek(previous_offset)
        chunk = handle.read()
        new_offset = handle.tell()

    signals: list[dict[str, Any]] = []
    for line in chunk.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            signals.append(payload)
    return signals, new_offset


def _write_signal_offset(run_dir: Path, offset: int) -> None:
    offset_path = run_dir / SIGNAL_OFFSET_FILE
    offset_path.write_text(str(max(offset, 0)))


def _normalize_signal_payload(signal: dict[str, Any]) -> Optional[dict[str, Any]]:
    payload = signal.get("payload")
    if not isinstance(payload, dict):
        return None
    message = payload.get("message")
    if not isinstance(message, str) or not message.strip():
        return None
    context = payload.get("context")
    if context is not None and not isinstance(context, dict):
        return None
    normalized: dict[str, Any] = {"message": message.strip()}
    if context is not None:
        normalized["context"] = context
    return normalized


def _apply_pending_signals(run_dir: Path, run_record: RunRecord) -> RunRecord:
    signals, new_offset = _read_pending_signals(run_dir)
    if not signals:
        queue_path = run_dir / SIGNAL_QUEUE_FILE
        if queue_path.exists():
            _write_signal_offset(run_dir, new_offset)
        return run_record
    run_log = run_dir / "run.log"
    updated = run_record
    for signal in signals:
        state = str(signal.get("state") or "").upper()
        if state != "NEEDS_INPUT":
            append_log(run_log, f"[loops] ignoring unsupported signal state: {state}")
            continue
        payload = _normalize_signal_payload(signal)
        if payload is None:
            append_log(run_log, "[loops] ignoring NEEDS_INPUT signal with invalid payload")
            continue
        append_log(run_log, "[loops] signal applied: NEEDS_INPUT")
        updated = replace(
            updated,
            needs_user_input=True,
            needs_user_input_payload=payload,
        )
    if updated == run_record:
        _write_signal_offset(run_dir, new_offset)
        return run_record
    written = write_run_record(run_dir / "run.json", updated)
    _write_signal_offset(run_dir, new_offset)
    return written


def _resolve_run_dir(run_dir: Optional[str]) -> Path:
    if run_dir:
        return Path(run_dir)
    env_run_dir = os.environ.get("LOOPS_RUN_DIR")
    if env_run_dir:
        return Path(env_run_dir)
    raise SystemExit("LOOPS_RUN_DIR is required (or pass --run-dir)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a Loops inner loop task.")
    parser.add_argument(
        "--run-dir",
        type=str,
        default=None,
        help="Path to the run directory (defaults to LOOPS_RUN_DIR).",
    )
    parser.add_argument(
        "--prompt-file",
        type=str,
        default=None,
        help="Optional path to a base prompt file.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Reset run.json to initial state and exit.",
    )
    args = parser.parse_args()
    run_dir = _resolve_run_dir(args.run_dir)
    prompt_file = Path(args.prompt_file) if args.prompt_file else None
    if args.reset:
        reset_run_record(run_dir)
        return
    run_inner_loop(run_dir, prompt_file=prompt_file)


if __name__ == "__main__":
    main()
