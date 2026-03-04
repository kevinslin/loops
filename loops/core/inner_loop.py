from __future__ import annotations

"""Inner loop runner for executing Codex with the unified prompt."""

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
from typing import Any, Callable, Mapping, Optional, cast

from loops.core.hooks import TransitionContext, build_default_hook_executor
from loops.core.outer_loop import build_provider, load_config
from loops.state.approval_config import DEFAULT_APPROVAL_COMMENT_PATTERN
from loops.core.handoff_handlers import (
    DEFAULT_HANDOFF_HANDLER,
    HANDOFF_HANDLER_STDIN,
    HandoffResult,
    resolve_builtin_handoff_handler,
    validate_handoff_handler_name,
)
from loops.state.inner_loop_runtime_config import (
    INNER_LOOP_RUNTIME_CONFIG_FILE,
    InnerLoopRuntimeConfig,
    read_inner_loop_runtime_config,
)
from loops.state.constants import (
    AGENT_LOG_FILE_NAME,
    CHECKOUT_MODE_WORKTREE,
    PUSH_PR_URL_FILE,
    RUN_LOG_FILE_NAME,
    RUN_RECORD_FILE_NAME,
    SIGNAL_OFFSET_FILE,
    STATE_HOOKS_LEDGER_FILE,
)
from loops.utils.logging import (
    STREAM_LOGS_STDOUT_ENV,
    append_log,
    reset_stream_logs_stdout_override,
    set_stream_logs_stdout_override,
    should_stream_logs_to_stdout,
)
from loops.state.run_record import (
    CodexSession,
    RunAutoApprove,
    RunPR,
    RunRecord,
    RunState,
    Task,
    derive_run_state,
    read_run_record,
    write_run_record,
)
from loops.task_providers.base import TaskProvider

PROMPT_TEMPLATE = (
    "Use the $dev.loop skill to implement the task and open a PR.\n"
    "You are running inside the loops test harness. Wait only for review from the "
    "a-review subagent. NEVER wait for human PR "
    "review/comments inside the agent; the harness monitors review activity and "
    "will re-invoke you when feedback arrives.\n"
    "When you run a-review, always post its response to the PR comments. "
    "If there are no findings, explicitly post that no issues were found.\n"
    "NEVER use the gen-notifier skill while running inside loops.\n"
    "Spawn the a-review subagent exactly once per conversation, only while state is "
    "<state>RUNNING</state>. Do not spawn a-review again in "
    "<state>WAITING_ON_REVIEW</state> or any later turn.\n"
    "The current inner-loop state is passed via a trailing <state>...</state> tag; "
    "initial state is <state>RUNNING</state>.\n"
    "Do not update issue/project task status directly; Loops applies deterministic "
    "status transitions when states change.\n"
    "If you need input from user, print what you need help with and end current conversation "
    "with <state>NEEDS_INPUT</>\n"
    "For the initial PR while state is <state>RUNNING</state>: if there are "
    "unstaged changes invoke:commit-code; then resolve REPO_ROOT from LOOPS_RUN_DIR "
    "and run python3 \"$REPO_ROOT/scripts/push-pr.py\" \"<pr-title>\" "
    "\"<pr-body-file>\"; then invoke:check-ci and if CI fails invoke:fix-pr.\n"
    "trigger:merge-pr when the state is exactly <state>PR_APPROVED</state>.\n"
    "Do not merge until the state is exactly <state>PR_APPROVED</state>.\n"
    "In the initial PR description, do not repeat the PR title in the body.\n"
    "Include session context in the initial PR body using: sessionid: [session]\n"
    "When posting PR progress comments, avoid duplicate messages by checking your latest "
    "PR comment before posting a new one.\n"
    "Do not reuse stock opener text (for example: 'Addressed the new discussion feedback'); "
    "write a specific update for the current change or skip commenting when nothing changed.\n"
    "When posting markdown comments with backticks via gh, use --body-file or a single-quoted "
    "heredoc to avoid shell interpolation issues.\n"
    "Task: {task}\n"
)
PROMPT_STATE_RUNNING = "RUNNING"
PROMPT_STATE_WAITING_ON_REVIEW = "WAITING_ON_REVIEW"
PROMPT_STATE_PR_APPROVED = "PR_APPROVED"
DEFAULT_MAX_ITERATIONS = 200
DEFAULT_REVIEW_POLL_SECONDS = 5.0
DEFAULT_MAX_REVIEW_POLL_SECONDS = 60.0
# With poll backoff (5s, 10s, 20s, 40s, then 60s), 49 idle polls is ~45m before escalation.
DEFAULT_MAX_IDLE_POLLS = 49
WAITING_STATES = {"WAITING_ON_REVIEW", "PR_APPROVED"}
GITHUB_PR_PATTERN = re.compile(
    r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/([0-9]+)"
)

SESSION_ID_PATTERN = re.compile(r"session[_\s-]*id\s*[:=]\s*([\w-]+)", re.IGNORECASE)
UUID_PATTERN = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
TRAILING_STATE_MARKER_PATTERN = re.compile(
    r"^\s*<state>\s*([A-Za-z_]+)\s*</(?:state)?>\s*$",
    re.IGNORECASE,
)
STATE_MARKER_VALUES = {"RUNNING", "WAITING_ON_REVIEW", "NEEDS_INPUT", "PR_APPROVED", "DONE"}
UserHandoffHandler = Callable[[dict[str, Any]], HandoffResult | str | None]
PRStatusFetcher = Callable[[RunPR], RunPR]
SleepFn = Callable[[float], None]
GH_PR_VIEW_JSON_FIELDS = (
    "reviewDecision,mergedAt,url,number,latestReviews,reviews,comments,statusCheckRollup"
)
CODEX_EXEC_SUBCOMMANDS = {"exec", "e"}
CODEX_LAUNCHER_SUBCOMMANDS = {"uv", "uvx"}
APPROVAL_REVIEW_STATES = {"COMMENTED", "APPROVED"}
CI_SUCCESS_CONCLUSIONS = {"SUCCESS", "NEUTRAL", "SKIPPED"}
CI_FAILURE_CONCLUSIONS = {
    "FAILURE",
    "CANCELLED",
    "TIMED_OUT",
    "ACTION_REQUIRED",
    "STARTUP_FAILURE",
    "STALE",
}
CI_PENDING_STATUSES = {"EXPECTED", "PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED"}
GH_ADD_REACTION_MUTATION = (
    "mutation($subjectId:ID!){"
    "addReaction(input:{subjectId:$subjectId,content:THUMBS_UP}){reaction{content}}"
    "}"
)


@dataclass(frozen=True)
class CommentApprovalSettings:
    allowed_usernames: tuple[str, ...]
    pattern_text: str
    approval_regex: re.Pattern[str]
    review_actor_usernames: tuple[str, ...] = ()
    used_default_pattern: bool = False

    @property
    def enabled(self) -> bool:
        return bool(self.allowed_usernames)


@dataclass(frozen=True)
class ApprovalSignal:
    timestamp: str
    author: str
    source: str
    event_node_id: str | None = None
    viewer_has_thumbs_up_reaction: bool = False


@dataclass(frozen=True)
class InnerLoopRuntimeContext:
    run_dir: Path
    run_id: str
    run_json_path: Path
    run_log: Path
    agent_log: Path
    environ: Mapping[str, str]
    command: list[str]
    base_prompt: Optional[str]
    user_handoff_handler: UserHandoffHandler
    pr_status_fetcher: PRStatusFetcher
    sleep_fn: SleepFn
    initial_poll_seconds: float
    max_poll_seconds: float
    max_idle_polls: int
    non_interactive_default_handoff: bool
    auto_approve_enabled: bool = False
    task_provider: TaskProvider | None = None


@dataclass
class LoopControlState:
    backoff_seconds: float
    idle_polls: int = 0
    next_user_response: Optional[str] = None
    cleanup_executed_for_pr: Optional[str] = None
    auto_approve_attempted: bool = False


@dataclass(frozen=True)
class StateHandlerResult:
    run_record: RunRecord
    action: str
    terminate: bool = False


def reset_run_record(run_dir: Path) -> RunRecord:
    """Reset run.json orchestration state while preserving durable identifiers."""

    resolved_run_dir = run_dir.resolve()
    run_json_path = resolved_run_dir / RUN_RECORD_FILE_NAME
    run_log = resolved_run_dir / RUN_LOG_FILE_NAME

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

    state_hook_ledger_path = resolved_run_dir / STATE_HOOKS_LEDGER_FILE
    if state_hook_ledger_path.exists():
        try:
            state_hook_ledger_path.unlink()
            append_log(
                run_log,
                f"[loops] removed state hook ledger during reset ({state_hook_ledger_path})",
            )
        except Exception as exc:
            append_log(
                run_log,
                (
                    "[loops] warning: failed to clear state hook ledger during reset "
                    f"({state_hook_ledger_path}): {exc}"
                ),
            )

    runtime_config = _load_runtime_config(run_dir=resolved_run_dir, run_log=run_log)
    runtime_env = runtime_config.env if runtime_config is not None else None
    runtime_environ = _apply_runtime_env_overrides(runtime_env)
    runtime_environ = _configure_log_streaming(
        runtime_config=runtime_config,
        environ=runtime_environ,
    )
    effective_stream_logs_stdout = should_stream_logs_to_stdout(environ=runtime_environ)
    reset_record = RunRecord(
        task=task,
        pr=_build_reset_pr(existing_record.pr if existing_record is not None else None),
        codex_session=None,
        needs_user_input=False,
        needs_user_input_payload=None,
        stream_logs_stdout=effective_stream_logs_stdout,
        checkout_mode=(
            existing_record.checkout_mode if existing_record is not None else "branch"
        ),
        starting_commit=(
            existing_record.starting_commit if existing_record is not None else "unknown"
        ),
        last_state="RUNNING",
        updated_at="",
    )
    written = write_run_record(
        run_json_path,
        reset_record,
        auto_approve_enabled=_load_auto_approve_enabled(
            runtime_auto_approve_enabled=(
                runtime_config.auto_approve_enabled if runtime_config is not None else None
            ),
            allow_env_fallback=True,
        ),
    )
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
    run_json_path = run_dir / RUN_RECORD_FILE_NAME
    run_log = run_dir / RUN_LOG_FILE_NAME
    agent_log = run_dir / AGENT_LOG_FILE_NAME
    runtime_config = _load_runtime_config(run_dir=run_dir, run_log=run_log)
    runtime_env = runtime_config.env if runtime_config is not None else None
    runtime_environ = _apply_runtime_env_overrides(runtime_env)
    runtime_environ = _configure_log_streaming(
        runtime_config=runtime_config,
        environ=runtime_environ,
    )
    # Ensure Codex turns always receive the current run directory even when the
    # inner loop is invoked directly (not via outer-loop launcher).
    runtime_environ["LOOPS_RUN_DIR"] = str(run_dir)
    effective_stream_logs_stdout = should_stream_logs_to_stdout(environ=runtime_environ)
    stream_logs_token = set_stream_logs_stdout_override(effective_stream_logs_stdout)

    try:
        base_prompt = _load_prompt_file(
            prompt_file,
            runtime_env=runtime_env,
            allow_env_fallback=True,
            environ=runtime_environ,
        )
        command = _resolve_codex_command(
            runtime_env=runtime_env,
            allow_env_fallback=True,
            environ=runtime_environ,
        )
        comment_approval = _load_comment_approval_settings(
            runtime_config=runtime_config,
        )
        if comment_approval.used_default_pattern:
            append_log(
                run_log,
                (
                    "[loops] invalid approval comment pattern in run runtime config; "
                    "falling back to default"
                ),
            )
        if pr_status_fetcher is None:

            def _default_pr_status_fetcher(pr: RunPR) -> RunPR:
                updated_pr, approved_by_comment, approved_by = _fetch_pr_status_with_gh_with_context(
                    pr,
                    comment_approval=comment_approval,
                    log_message=lambda message: append_log(run_log, message),
                    environ=runtime_environ,
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
        configured_handoff_handler = _resolve_configured_handoff_handler_name(
            runtime_handoff_handler=(
                runtime_config.handoff_handler if runtime_config is not None else None
            ),
            allow_env_fallback=True,
            environ=runtime_environ,
        )
        auto_approve_enabled = _load_auto_approve_enabled(
            runtime_auto_approve_enabled=(
                runtime_config.auto_approve_enabled if runtime_config is not None else None
            ),
            allow_env_fallback=True,
            environ=runtime_environ,
        )
        initial_run_record = read_run_record(run_json_path)
        task_provider = _resolve_task_provider_for_run(
            run_dir=run_dir,
            task=initial_run_record.task,
            run_log=run_log,
        )
        if initial_run_record.stream_logs_stdout != effective_stream_logs_stdout:
            initial_run_record = write_run_record(
                run_json_path,
                replace(
                    initial_run_record,
                    stream_logs_stdout=effective_stream_logs_stdout,
                ),
                auto_approve_enabled=auto_approve_enabled,
            )
        if user_handoff_handler is None:
            user_handoff_handler = resolve_builtin_handoff_handler(
                configured_handoff_handler,
                run_dir=run_dir,
                task=initial_run_record.task,
                stdin_handler=_default_user_handoff_handler,
                log_message=lambda message: append_log(run_log, message),
                environ=runtime_environ,
            )
            selected_handoff_handler = configured_handoff_handler
            using_default_handoff_handler = selected_handoff_handler == HANDOFF_HANDLER_STDIN
        else:
            selected_handoff_handler = "custom_handler"
            using_default_handoff_handler = False
        append_log(run_log, f"[loops] using handoff handler: {selected_handoff_handler}")
        non_interactive_default_handoff = (
            using_default_handoff_handler and not _has_interactive_stdin()
        )
        runtime = InnerLoopRuntimeContext(
            run_dir=run_dir,
            run_id=_build_run_id(run_dir),
            run_json_path=run_json_path,
            run_log=run_log,
            agent_log=agent_log,
            environ=runtime_environ,
            command=command,
            base_prompt=base_prompt,
            user_handoff_handler=user_handoff_handler,
            pr_status_fetcher=pr_status_fetcher,
            sleep_fn=sleep_fn,
            initial_poll_seconds=initial_poll_seconds,
            max_poll_seconds=max_poll_seconds,
            max_idle_polls=max_idle_polls,
            non_interactive_default_handoff=non_interactive_default_handoff,
            auto_approve_enabled=auto_approve_enabled,
            task_provider=task_provider,
        )
        control = LoopControlState(backoff_seconds=initial_poll_seconds)

        def hook_logger(message: str) -> None:
            append_log(run_log, message)

        hook_executor = build_default_hook_executor(
            run_dir=run_dir,
            logger=hook_logger,
        )

        for iteration in range(1, max_iterations + 1):
            run_record = read_run_record(run_json_path)
            state = _derive_state(
                run_record,
                auto_approve_enabled=runtime.auto_approve_enabled,
            )
            _log_iteration_enter(
                run_log,
                iteration=iteration,
                state=state,
                run_record=run_record,
                backoff_seconds=control.backoff_seconds,
                idle_polls=control.idle_polls,
            )
            transition_context = TransitionContext(
                run_id=runtime.run_id,
                task_id=run_record.task.id,
                task_provider=runtime.task_provider,
                from_state=run_record.last_state,
                to_state=state,
                logger=hook_logger,
            )
            hook_executor.execute_on_enter(state=state, context=transition_context)

            transition: StateHandlerResult | None = None
            done_record: RunRecord | None = None
            try:
                if state == "DONE":
                    append_log(run_log, "[loops] run state DONE; exiting inner loop")
                    done_record = run_record
                else:
                    transition = _handle_state(
                        state=state,
                        run_record=run_record,
                        runtime=runtime,
                        control=control,
                    )
            finally:
                hook_executor.execute_on_exit(state=state, context=transition_context)

            if done_record is not None:
                _log_iteration_exit(
                    run_log,
                    iteration=iteration,
                    next_state=state,
                    run_record=done_record,
                    action="done_exit",
                    backoff_seconds=control.backoff_seconds,
                    idle_polls=control.idle_polls,
                )
                return done_record

            if transition is None:
                raise RuntimeError(f"state handler returned no result for state={state}")
            next_state = _derive_state(
                transition.run_record,
                auto_approve_enabled=runtime.auto_approve_enabled,
            )
            _log_iteration_exit(
                run_log,
                iteration=iteration,
                next_state=next_state,
                run_record=transition.run_record,
                action=transition.action,
                backoff_seconds=control.backoff_seconds,
                idle_polls=control.idle_polls,
            )
            if transition.terminate:
                return transition.run_record

        final_record = read_run_record(run_json_path)
        final_record = _force_needs_input(
            run_json_path,
            final_record,
            message=(
                "Inner loop reached max iterations without DONE. "
                "Please provide guidance."
            ),
            auto_approve_enabled=runtime.auto_approve_enabled,
        )
        append_log(
            run_log,
            (
                "[loops] iteration limit reached; forcing NEEDS_INPUT "
                f"(max_iterations={max_iterations})"
            ),
        )
        return final_record
    finally:
        reset_stream_logs_stdout_override(stream_logs_token)


def _handle_state(
    *,
    state: str,
    run_record: RunRecord,
    runtime: InnerLoopRuntimeContext,
    control: LoopControlState,
) -> StateHandlerResult:
    if state == "NEEDS_INPUT":
        return _handle_needs_input_state(
            run_record=run_record,
            runtime=runtime,
            control=control,
        )
    if state == "RUNNING":
        return _handle_running_state(
            run_record=run_record,
            runtime=runtime,
            control=control,
        )
    if state == "WAITING_ON_REVIEW":
        return _handle_waiting_on_review_state(
            run_record=run_record,
            runtime=runtime,
            control=control,
        )
    if state == "PR_APPROVED":
        return _handle_pr_approved_state(
            run_record=run_record,
            runtime=runtime,
            control=control,
        )
    raise ValueError(f"unsupported state: {state}")


def _handle_needs_input_state(
    *,
    run_record: RunRecord,
    runtime: InnerLoopRuntimeContext,
    control: LoopControlState,
) -> StateHandlerResult:
    response = _handle_needs_input(
        run_record,
        runtime.user_handoff_handler,
        runtime.run_log,
    )
    if response is None:
        if runtime.non_interactive_default_handoff:
            append_log(
                runtime.run_log,
                "[loops] non-interactive mode; exiting while waiting for user input",
            )
            terminal_record = read_run_record(runtime.run_json_path)
            return StateHandlerResult(
                run_record=terminal_record,
                action="needs_input_non_interactive_exit",
                terminate=True,
            )
        _sleep_with_backoff(control=control, runtime=runtime)
        return StateHandlerResult(
            run_record=run_record,
            action="needs_input_waiting",
        )

    control.next_user_response = response
    cleared_record = write_run_record(
        runtime.run_json_path,
        replace(
            run_record,
            needs_user_input=False,
            needs_user_input_payload=None,
        ),
        auto_approve_enabled=runtime.auto_approve_enabled,
    )
    control.backoff_seconds = runtime.initial_poll_seconds
    control.idle_polls = 0
    return StateHandlerResult(
        run_record=cleared_record,
        action="needs_input_cleared",
    )


def _handle_running_state(
    *,
    run_record: RunRecord,
    runtime: InnerLoopRuntimeContext,
    control: LoopControlState,
) -> StateHandlerResult:
    updated_record = _run_codex_turn(
        run_json_path=runtime.run_json_path,
        run_log=runtime.run_log,
        agent_log=runtime.agent_log,
        run_record=run_record,
        command=runtime.command,
        environ=runtime.environ,
        base_prompt=runtime.base_prompt,
        user_response=control.next_user_response,
        review_feedback=False,
        auto_approve_enabled=runtime.auto_approve_enabled,
    )
    control.next_user_response = None
    control.cleanup_executed_for_pr = None
    control.backoff_seconds = runtime.initial_poll_seconds
    control.idle_polls = 0
    return StateHandlerResult(run_record=updated_record, action="codex_turn")


def _handle_waiting_on_review_state(
    *,
    run_record: RunRecord,
    runtime: InnerLoopRuntimeContext,
    control: LoopControlState,
) -> StateHandlerResult:
    if run_record.pr is None:
        missing_pr_record = _force_needs_input(
            runtime.run_json_path,
            run_record,
            message="Run is waiting on review but no PR metadata exists.",
            auto_approve_enabled=runtime.auto_approve_enabled,
        )
        return StateHandlerResult(
            run_record=missing_pr_record,
            action="review_missing_pr",
        )
    try:
        updated_pr = runtime.pr_status_fetcher(run_record.pr)
    except Exception as exc:
        append_log(runtime.run_log, f"[loops] failed to poll PR status: {exc}")
        previous_state = _derive_state(
            run_record,
            auto_approve_enabled=runtime.auto_approve_enabled,
        )
        run_record, control.idle_polls = _increment_idle_polls(
            run_json_path=runtime.run_json_path,
            run_record=run_record,
            idle_polls=control.idle_polls,
            max_idle_polls=runtime.max_idle_polls,
            message=(
                "PR polling has been idle for too long. "
                "Please check review status manually."
            ),
            auto_approve_enabled=runtime.auto_approve_enabled,
        )
        next_state = _derive_state(
            run_record,
            auto_approve_enabled=runtime.auto_approve_enabled,
        )
        if next_state not in WAITING_STATES:
            return StateHandlerResult(run_record=run_record, action="review_poll_error")
        if previous_state != next_state:
            control.backoff_seconds = runtime.initial_poll_seconds
            control.idle_polls = 0
            return StateHandlerResult(run_record=run_record, action="review_poll_error")
        _sleep_with_backoff(control=control, runtime=runtime)
        return StateHandlerResult(run_record=run_record, action="review_poll_error")

    run_record = write_run_record(
        runtime.run_json_path,
        replace(run_record, pr=updated_pr),
        auto_approve_enabled=runtime.auto_approve_enabled,
    )
    if run_record.pr is not None and _should_resume_review_feedback(run_record.pr):
        if run_record.pr.review_status == "changes_requested":
            append_log(runtime.run_log, "[loops] review changes requested; resuming codex")
        else:
            append_log(runtime.run_log, "[loops] new PR comment feedback detected; resuming codex")
        run_record = _run_codex_turn(
            run_json_path=runtime.run_json_path,
            run_log=runtime.run_log,
            agent_log=runtime.agent_log,
            run_record=run_record,
            command=runtime.command,
            environ=runtime.environ,
            base_prompt=runtime.base_prompt,
            user_response=control.next_user_response,
            review_feedback=True,
            auto_approve_enabled=runtime.auto_approve_enabled,
        )
        control.next_user_response = None
        control.cleanup_executed_for_pr = None
        control.backoff_seconds = runtime.initial_poll_seconds
        control.idle_polls = 0
        return StateHandlerResult(
            run_record=run_record,
            action="review_feedback_codex_turn",
        )

    if runtime.auto_approve_enabled and run_record.pr is not None:
        if run_record.pr.ci_status == "success":
            review_status = run_record.pr.review_status or "open"
            # Keep the old manual-approval behavior: if already approved, derive PR_APPROVED
            # directly. Auto-approve evaluation applies to any not-yet-approved state.
            if review_status != "approved":
                verdict = (
                    run_record.auto_approve.verdict
                    if run_record.auto_approve is not None
                    else "none"
                )
                if verdict == "none":
                    if control.auto_approve_attempted:
                        append_log(
                            runtime.run_log,
                            (
                                "[loops] auto-approve evaluation already attempted in this "
                                "conversation; waiting for manual guidance"
                            ),
                        )
                    else:
                        run_record = _run_auto_approve_eval(
                            run_record=run_record,
                            runtime=runtime,
                            control=control,
                        )
                        control.auto_approve_attempted = True
                        control.backoff_seconds = runtime.initial_poll_seconds
                        control.idle_polls = 0
                        return StateHandlerResult(
                            run_record=run_record,
                            action="auto_approve_eval",
                        )
                else:
                    control.auto_approve_attempted = True

    if (
        _derive_state(
            run_record,
            auto_approve_enabled=runtime.auto_approve_enabled,
        )
        == "WAITING_ON_REVIEW"
    ):
        run_record, control.idle_polls = _increment_idle_polls(
            run_json_path=runtime.run_json_path,
            run_record=run_record,
            idle_polls=control.idle_polls,
            max_idle_polls=runtime.max_idle_polls,
            message=(
                "PR has not changed after repeated polls. "
                "Please provide manual guidance."
            ),
            auto_approve_enabled=runtime.auto_approve_enabled,
        )
        next_state = _derive_state(
            run_record,
            auto_approve_enabled=runtime.auto_approve_enabled,
        )
        if next_state not in WAITING_STATES:
            return StateHandlerResult(run_record=run_record, action="review_poll")
        if next_state != "WAITING_ON_REVIEW":
            control.backoff_seconds = runtime.initial_poll_seconds
            control.idle_polls = 0
            return StateHandlerResult(run_record=run_record, action="review_poll")
    else:
        control.idle_polls = 0
        control.backoff_seconds = runtime.initial_poll_seconds

    _sleep_with_backoff(control=control, runtime=runtime)
    return StateHandlerResult(run_record=run_record, action="review_poll")


def _handle_pr_approved_state(
    *,
    run_record: RunRecord,
    runtime: InnerLoopRuntimeContext,
    control: LoopControlState,
) -> StateHandlerResult:
    if run_record.pr is None:
        missing_pr_record = _force_needs_input(
            runtime.run_json_path,
            run_record,
            message="Run is PR_APPROVED but no PR metadata exists.",
            auto_approve_enabled=runtime.auto_approve_enabled,
        )
        return StateHandlerResult(
            run_record=missing_pr_record,
            action="approved_missing_pr",
        )

    if control.cleanup_executed_for_pr != run_record.pr.url:
        cleanup_prompt = _build_cleanup_prompt(run_record.task.url, runtime.base_prompt)
        output, exit_code, _resume_fallback_used = _invoke_codex(
            base_command=runtime.command,
            prompt=cleanup_prompt,
            agent_log=runtime.agent_log,
            run_log=runtime.run_log,
            codex_session=run_record.codex_session,
            turn_label="cleanup turn",
            environ=runtime.environ,
        )
        append_log(runtime.run_log, output)
        if exit_code != 0:
            failed_cleanup_record = _force_needs_input(
                runtime.run_json_path,
                run_record,
                message="Cleanup failed after PR approval. Please advise.",
                context={"exit_code": exit_code},
                auto_approve_enabled=runtime.auto_approve_enabled,
            )
            return StateHandlerResult(
                run_record=failed_cleanup_record,
                action="cleanup_failed",
            )
        control.cleanup_executed_for_pr = run_record.pr.url

    try:
        updated_pr = runtime.pr_status_fetcher(run_record.pr)
    except Exception as exc:
        append_log(runtime.run_log, f"[loops] failed to poll merge status: {exc}")
        previous_state = _derive_state(
            run_record,
            auto_approve_enabled=runtime.auto_approve_enabled,
        )
        run_record, control.idle_polls = _increment_idle_polls(
            run_json_path=runtime.run_json_path,
            run_record=run_record,
            idle_polls=control.idle_polls,
            max_idle_polls=runtime.max_idle_polls,
            message=(
                "Merge polling has been idle for too long. "
                "Please check merge status manually."
            ),
            auto_approve_enabled=runtime.auto_approve_enabled,
        )
        next_state = _derive_state(
            run_record,
            auto_approve_enabled=runtime.auto_approve_enabled,
        )
        if next_state not in WAITING_STATES:
            return StateHandlerResult(run_record=run_record, action="merge_poll_error")
        if previous_state != next_state:
            control.backoff_seconds = runtime.initial_poll_seconds
            control.idle_polls = 0
            return StateHandlerResult(run_record=run_record, action="merge_poll_error")
        _sleep_with_backoff(control=control, runtime=runtime)
        return StateHandlerResult(run_record=run_record, action="merge_poll_error")

    run_record = write_run_record(
        runtime.run_json_path,
        replace(run_record, pr=updated_pr),
        auto_approve_enabled=runtime.auto_approve_enabled,
    )
    previous_state = _derive_state(
        run_record,
        auto_approve_enabled=runtime.auto_approve_enabled,
    )
    if (
        _derive_state(
            run_record,
            auto_approve_enabled=runtime.auto_approve_enabled,
        )
        == "PR_APPROVED"
    ):
        run_record, control.idle_polls = _increment_idle_polls(
            run_json_path=runtime.run_json_path,
            run_record=run_record,
            idle_polls=control.idle_polls,
            max_idle_polls=runtime.max_idle_polls,
            message=(
                "PR is still approved but not merged after repeated polls. "
                "Please provide manual guidance."
            ),
            auto_approve_enabled=runtime.auto_approve_enabled,
        )
        next_state = _derive_state(
            run_record,
            auto_approve_enabled=runtime.auto_approve_enabled,
        )
        if next_state not in WAITING_STATES:
            return StateHandlerResult(run_record=run_record, action="approved_poll")

    next_state = _derive_state(
        run_record,
        auto_approve_enabled=runtime.auto_approve_enabled,
    )
    if (
        previous_state == "PR_APPROVED"
        and next_state in WAITING_STATES
        and next_state != previous_state
    ):
        control.backoff_seconds = runtime.initial_poll_seconds
        control.idle_polls = 0
        return StateHandlerResult(run_record=run_record, action="approved_poll")
    if next_state not in WAITING_STATES:
        return StateHandlerResult(run_record=run_record, action="approved_poll")

    _sleep_with_backoff(control=control, runtime=runtime)
    return StateHandlerResult(run_record=run_record, action="approved_poll")


def _sleep_with_backoff(
    *,
    control: LoopControlState,
    runtime: InnerLoopRuntimeContext,
) -> None:
    runtime.sleep_fn(min(control.backoff_seconds, runtime.max_poll_seconds))
    control.backoff_seconds = min(
        control.backoff_seconds * 2,
        runtime.max_poll_seconds,
    )


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
        ci_status=None,
        ci_last_checked_at=None,
        merged_at=None,
        last_checked_at=None,
        latest_review_submitted_at=None,
        review_addressed_at=None,
    )


def _build_run_id(run_dir: Path) -> str:
    return str(run_dir.resolve())


def _resolve_task_provider_for_run(
    *,
    run_dir: Path,
    task: Task,
    run_log: Path,
) -> TaskProvider | None:
    loops_root = _resolve_loops_root_from_run_dir(run_dir)
    if loops_root is None:
        append_log(
            run_log,
            "[loops] state hooks: provider resolution skipped (missing .loops root)",
        )
        return None

    config_path = loops_root / "config.json"
    if not config_path.exists():
        append_log(
            run_log,
            (
                "[loops] state hooks: provider resolution skipped "
                f"(missing config {config_path})"
            ),
        )
        return None

    try:
        config = load_config(config_path)
    except Exception as exc:
        append_log(
            run_log,
            f"[loops] state hooks: failed to load config for provider resolution: {exc}",
        )
        return None

    if config.task_provider_id != task.provider_id:
        append_log(
            run_log,
            (
                "[loops] state hooks: provider resolution skipped "
                f"(task provider_id={task.provider_id!r} "
                f"config provider_id={config.task_provider_id!r})"
            ),
        )
        return None

    try:
        return build_provider(config)
    except Exception as exc:
        append_log(
            run_log,
            f"[loops] state hooks: failed to build provider for status hooks: {exc}",
        )
        return None


def _resolve_loops_root_from_run_dir(run_dir: Path) -> Path | None:
    for candidate in (run_dir, *run_dir.parents):
        if candidate.name == ".loops":
            return candidate
    return None


def _load_runtime_config(
    *,
    run_dir: Path,
    run_log: Path,
) -> Optional[InnerLoopRuntimeConfig]:
    try:
        return read_inner_loop_runtime_config(run_dir)
    except Exception as exc:
        runtime_config_path = run_dir / INNER_LOOP_RUNTIME_CONFIG_FILE
        if runtime_config_path.exists():
            append_log(
                run_log,
                (
                    "[loops] failed to load run runtime config; "
                    f"aborting: {exc}"
                ),
            )
        raise


def _apply_runtime_env_overrides(runtime_env: Mapping[str, str] | None) -> dict[str, str]:
    merged = os.environ.copy()
    if runtime_env:
        merged.update(runtime_env)
    return merged


def _configure_log_streaming(
    *,
    runtime_config: Optional[InnerLoopRuntimeConfig],
    environ: Mapping[str, str],
) -> dict[str, str]:
    configured = dict(environ)
    if runtime_config is None or runtime_config.stream_logs_stdout is None:
        return configured
    if runtime_config.stream_logs_stdout:
        configured[STREAM_LOGS_STDOUT_ENV] = "1"
        return configured
    configured.pop(STREAM_LOGS_STDOUT_ENV, None)
    return configured


def _derive_state(
    run_record: RunRecord,
    *,
    auto_approve_enabled: bool,
) -> str:
    return derive_run_state(
        run_record.pr,
        run_record.needs_user_input,
        auto_approve_enabled=auto_approve_enabled,
        auto_approve=run_record.auto_approve,
    )


def _load_auto_approve_enabled(
    *,
    runtime_auto_approve_enabled: bool | None = None,
    allow_env_fallback: bool = True,
    environ: Mapping[str, str] | None = None,
) -> bool:
    if runtime_auto_approve_enabled is not None:
        return runtime_auto_approve_enabled
    if not allow_env_fallback:
        return False
    source = os.environ if environ is None else environ
    raw = source.get("LOOPS_AUTO_APPROVE_ENABLED")
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


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
        pr_summary = "pr_status=none ci_status=none pr_number=- pr_merged=no"
    else:
        review_status = pr.review_status or "unknown"
        ci_status = pr.ci_status or "unknown"
        pr_number = pr.number if pr.number is not None else "-"
        pr_merged = "yes" if pr.merged_at else "no"
        pr_summary = (
            f"pr_status={review_status} ci_status={ci_status} "
            f"pr_number={pr_number} pr_merged={pr_merged}"
        )
    auto_approve_verdict = (
        run_record.auto_approve.verdict if run_record.auto_approve is not None else "none"
    )
    return (
        f"needs_user_input={run_record.needs_user_input} "
        f"auto_approve={auto_approve_verdict} {pr_summary}"
    )


def _resolve_codex_command(
    *,
    runtime_env: Mapping[str, str] | None = None,
    allow_env_fallback: bool = True,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    raw_command = None
    if runtime_env is not None:
        raw_command = runtime_env.get("CODEX_CMD")
    if raw_command is None and allow_env_fallback:
        source = os.environ if environ is None else environ
        raw_command = source.get("CODEX_CMD")
    if raw_command is None:
        raw_command = "codex exec --yolo"
    command = shlex.split(raw_command)
    if not command:
        raise ValueError("CODEX_CMD cannot be empty")
    return command


def _build_codex_turn_command(
    base_command: list[str],
    *,
    codex_session: Optional[CodexSession],
) -> tuple[list[str], str]:
    if codex_session is None:
        return list(base_command), "new"
    resume_command = _inject_codex_resume_command(base_command, codex_session.id)
    if resume_command is None:
        return list(base_command), "resume_unsupported"
    return resume_command, "resume"


def _inject_codex_resume_command(
    base_command: list[str],
    session_id: str,
) -> Optional[list[str]]:
    command = list(base_command)
    codex_index = _find_codex_token_index(command)
    if codex_index is None:
        return None
    exec_index = codex_index + 1
    if len(command) <= exec_index or command[exec_index] not in CODEX_EXEC_SUBCOMMANDS:
        return None
    if "resume" in command[exec_index + 1 :]:
        return command
    return [*command, "resume", session_id]


def _find_codex_token_index(command: list[str]) -> Optional[int]:
    if not command:
        return None
    first = Path(command[0]).name.lower()
    if first == "codex":
        return 0
    if (
        first in CODEX_LAUNCHER_SUBCOMMANDS
        and len(command) > 1
        and Path(command[1]).name.lower() == "codex"
    ):
        return 1
    if first.startswith("python") and len(command) > 2:
        if command[1] == "-m" and command[2] == "codex":
            return 2
    return None


def _load_prompt_file(
    prompt_file: Optional[Path],
    *,
    runtime_env: Mapping[str, str] | None = None,
    allow_env_fallback: bool = True,
    environ: Mapping[str, str] | None = None,
) -> Optional[str]:
    if prompt_file is None:
        prompt_path = None
        if runtime_env is not None:
            prompt_path = runtime_env.get("LOOPS_PROMPT_FILE") or runtime_env.get(
                "CODEX_PROMPT_FILE"
            )
        if prompt_path is None and allow_env_fallback:
            source = os.environ if environ is None else environ
            prompt_path = source.get("LOOPS_PROMPT_FILE") or source.get(
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
    state: Optional[str] = PROMPT_STATE_RUNNING,
    checkout_mode: str = "branch",
    include_checkout_setup_instruction: bool = False,
) -> str:
    prompt = PROMPT_TEMPLATE.format(task=task_url)
    if include_checkout_setup_instruction:
        checkout_setup_instruction = _build_checkout_mode_setup_instruction(
            checkout_mode
        )
        if checkout_setup_instruction is not None:
            prompt += f"\n{checkout_setup_instruction}\n"
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


def _build_checkout_mode_setup_instruction(checkout_mode: str) -> str | None:
    if checkout_mode.strip().casefold() != CHECKOUT_MODE_WORKTREE:
        return None
    return (
        "Before making code changes, create and switch to a new git worktree for this "
        "task and complete implementation from that worktree."
    )


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


def _build_comment_feedback_prompt(
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
        f"\nPR {pr_url} has new discussion comments. Review the feedback, address "
        "requested changes, update the PR, and summarize what changed. If there "
        "are no changes requested, summarize that and end the current turn.\n"
    )
    return _append_state_tag(prompt, PROMPT_STATE_WAITING_ON_REVIEW)


def _build_auto_approve_eval_prompt(
    task_url: str,
    base_prompt: Optional[str],
    pr_url: str,
) -> str:
    prompt = _build_prompt(
        task_url,
        base_prompt,
        state=None,
    )
    prompt += (
        f"\nPR {pr_url} is not yet review-approved and has green CI. "
        "Run $ag-judge (judge book: references/jb.coding.md) against current diff, "
        "review threads, and CI evidence. Post the ag-judge verdict and impact/risk/size "
        "scores to the PR comments. Return exactly one JSON object on one line "
        'with keys: {"verdict":"APPROVE|REJECT|ESCALATE","impact":1-5,'
        '"risk":1-5,"size":1-5,"summary":"..."}.\n'
    )
    return _append_state_tag(prompt, PROMPT_STATE_WAITING_ON_REVIEW)


def _run_codex(
    command: list[str],
    prompt: str,
    agent_log: Path,
    *,
    environ: Mapping[str, str],
) -> tuple[str, int]:
    agent_log.parent.mkdir(parents=True, exist_ok=True)
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=dict(environ),
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


def _invoke_codex(
    *,
    base_command: list[str],
    prompt: str,
    agent_log: Path,
    run_log: Path,
    codex_session: Optional[CodexSession],
    turn_label: str,
    environ: Mapping[str, str],
) -> tuple[str, int, bool]:
    command, strategy = _build_codex_turn_command(
        base_command,
        codex_session=codex_session,
    )
    if strategy == "resume":
        append_log(
            run_log,
            f"[loops] {turn_label}: resuming codex session {codex_session.id}",
        )
    elif strategy == "resume_unsupported":
        append_log(
            run_log,
            (
                f"[loops] {turn_label}: session id present but CODEX_CMD does not "
                "support automatic resume; running base command"
            ),
        )
    else:
        append_log(run_log, f"[loops] {turn_label}: starting new codex session")
    output, exit_code = _run_codex(
        command,
        prompt,
        agent_log,
        environ=environ,
    )
    if strategy != "resume" or exit_code == 0:
        return output, exit_code, False

    append_log(
        run_log,
        (
            f"[loops] {turn_label}: resume command failed "
            f"(exit_code={exit_code}); retrying without resume"
        ),
    )
    fallback_output, fallback_exit_code = _run_codex(
        list(base_command),
        prompt,
        agent_log,
        environ=environ,
    )
    if fallback_exit_code == 0:
        append_log(
            run_log,
            f"[loops] {turn_label}: fallback without resume succeeded",
        )
    else:
        append_log(
            run_log,
            (
                f"[loops] {turn_label}: fallback without resume failed "
                f"(exit_code={fallback_exit_code})"
            ),
        )
    merged_output = output
    if merged_output and not merged_output.endswith("\n"):
        merged_output += "\n"
    merged_output += fallback_output
    return merged_output, fallback_exit_code, True


def _extract_session_id(output: str) -> Optional[str]:
    json_session_id: Optional[str] = None
    for line in output.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            if "session_id" in payload:
                json_session_id = str(payload["session_id"])
            if "session" in payload:
                json_session_id = str(payload["session"])
    if json_session_id is not None:
        return json_session_id
    session_id_matches = list(SESSION_ID_PATTERN.finditer(output))
    if session_id_matches:
        return session_id_matches[-1].group(1)
    uuid_matches = list(UUID_PATTERN.finditer(output))
    if uuid_matches:
        return uuid_matches[-1].group(0)
    return None


def _run_codex_turn(
    *,
    run_json_path: Path,
    run_log: Path,
    agent_log: Path,
    run_record: RunRecord,
    command: list[str],
    environ: Mapping[str, str],
    base_prompt: Optional[str],
    user_response: Optional[str] = None,
    review_feedback: bool,
    auto_approve_enabled: bool = False,
) -> RunRecord:
    if user_response is not None:
        normalized_response = user_response.strip()
        append_log(
            run_log,
            f"[loops] user input for codex turn: "
            f"present={bool(normalized_response)} length={len(normalized_response)}",
        )

    if review_feedback and run_record.pr is not None:
        if run_record.pr.review_status == "changes_requested":
            prompt = _build_review_feedback_prompt(
                run_record.task.url,
                base_prompt,
                run_record.pr.url,
                user_response=user_response,
            )
        else:
            prompt = _build_comment_feedback_prompt(
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
            checkout_mode=run_record.checkout_mode,
            include_checkout_setup_instruction=run_record.codex_session is None,
        )

    output, exit_code, resume_fallback_used = _invoke_codex(
        base_command=command,
        prompt=prompt,
        agent_log=agent_log,
        run_log=run_log,
        codex_session=run_record.codex_session,
        turn_label="codex turn",
        environ=environ,
    )
    append_log(run_log, output)

    session_id = _extract_session_id(output)
    codex_session = run_record.codex_session
    if session_id is not None:
        codex_session = CodexSession(id=session_id, last_prompt=prompt)
    elif resume_fallback_used:
        # Drop stale session ids when resume failed and no replacement was emitted.
        codex_session = None
    elif exit_code == 0 and codex_session is None:
        append_log(run_log, "[loops] warning: no session id detected in codex output")

    pr = run_record.pr
    requested_state = _extract_trailing_state_marker(output)
    if requested_state is not None:
        append_log(run_log, f"[loops] codex requested state via marker: {requested_state}")
    needs_user_input = exit_code != 0
    needs_user_input_payload = run_record.needs_user_input_payload
    if exit_code != 0:
        append_log(run_log, f"[loops] codex exit code {exit_code}")
        needs_user_input_payload = {
            "message": "Codex exited with a non-zero status. Provide guidance.",
            "context": {"exit_code": exit_code},
        }
    elif requested_state == "NEEDS_INPUT":
        needs_user_input = True
        needs_user_input_payload = {
            "message": (
                "Codex requested user input via trailing state marker. "
                "Provide guidance."
            )
        }
    elif not review_feedback and run_record.pr is None:
        pr = _extract_pr_from_push_pr_artifact(run_json_path.parent)
        if pr is None:
            pr = _extract_pr_from_user_response(user_response)
            if pr is not None:
                append_log(
                    run_log,
                    "[loops] deterministic PR discovery recovered from user input PR URL",
                )
        if pr is None:
            needs_user_input = True
            needs_user_input_payload = {
                "message": (
                    "Loops could not determine a PR URL from push-pr.py artifact output. "
                    "Provide the PR URL or rerun push-pr.py."
                ),
                "context": {
                    "artifact_path": str(run_json_path.parent / PUSH_PR_URL_FILE),
                },
            }
            append_log(
                run_log,
                (
                    "[loops] deterministic PR discovery failed "
                    f"(artifact={run_json_path.parent / PUSH_PR_URL_FILE}); "
                    "requesting user input"
                ),
            )
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
        auto_approve_enabled=auto_approve_enabled,
    )


def _parse_auto_approve_score(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    parsed: Optional[int]
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
    else:
        return None
    if parsed < 1 or parsed > 5:
        return None
    return parsed


def _extract_auto_approve_from_output(
    output: str,
    *,
    judged_at: str,
) -> RunAutoApprove:
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        verdict_raw = payload.get("verdict")
        if not isinstance(verdict_raw, str):
            continue
        verdict = verdict_raw.strip().upper()
        if verdict not in {"APPROVE", "REJECT", "ESCALATE"}:
            continue
        summary = payload.get("summary")
        return RunAutoApprove(
            verdict=verdict,
            impact=_parse_auto_approve_score(payload.get("impact")),
            risk=_parse_auto_approve_score(payload.get("risk")),
            size=_parse_auto_approve_score(payload.get("size")),
            judged_at=judged_at,
            summary=summary if isinstance(summary, str) else None,
        )

    verdict_match = re.search(r"\b(APPROVE|REJECT|ESCALATE)\b", output, re.IGNORECASE)
    if verdict_match is not None:
        verdict = verdict_match.group(1).upper()
        summary = output.strip().splitlines()[-1] if output.strip() else None
        return RunAutoApprove(
            verdict=verdict, judged_at=judged_at, summary=summary
        )

    return RunAutoApprove(
        verdict="ESCALATE",
        judged_at=judged_at,
        summary="Failed to parse auto-approve verdict from ag-judge output.",
    )


def _run_auto_approve_eval(
    *,
    run_record: RunRecord,
    runtime: InnerLoopRuntimeContext,
    control: LoopControlState,
) -> RunRecord:
    del control  # Reserved for future policy hooks.
    if run_record.pr is None:
        return run_record
    prompt = _build_auto_approve_eval_prompt(
        run_record.task.url,
        runtime.base_prompt,
        run_record.pr.url,
    )
    output, exit_code, resume_fallback_used = _invoke_codex(
        base_command=runtime.command,
        prompt=prompt,
        agent_log=runtime.agent_log,
        run_log=runtime.run_log,
        codex_session=run_record.codex_session,
        turn_label="auto-approve evaluation turn",
        environ=runtime.environ,
    )
    append_log(runtime.run_log, output)

    session_id = _extract_session_id(output)
    codex_session = run_record.codex_session
    if session_id is not None:
        codex_session = CodexSession(id=session_id, last_prompt=prompt)
    elif resume_fallback_used:
        codex_session = None

    judged_at = datetime.now(timezone.utc).isoformat()
    if exit_code != 0:
        append_log(
            runtime.run_log,
            f"[loops] auto-approve evaluation failed with exit code {exit_code}",
        )
        auto_approve = RunAutoApprove(
            verdict="ESCALATE",
            judged_at=judged_at,
            summary=f"ag-judge execution failed (exit_code={exit_code}).",
        )
    else:
        auto_approve = _extract_auto_approve_from_output(output, judged_at=judged_at)

    append_log(
        runtime.run_log,
        (
            "[loops] auto-approve verdict persisted: "
            f"verdict={auto_approve.verdict} "
            f"impact={auto_approve.impact or '-'} "
            f"risk={auto_approve.risk or '-'} "
            f"size={auto_approve.size or '-'}"
        ),
    )
    return write_run_record(
        runtime.run_json_path,
        replace(
            run_record,
            codex_session=codex_session,
            auto_approve=auto_approve,
        ),
        auto_approve_enabled=runtime.auto_approve_enabled,
    )


def _extract_pr_from_push_pr_artifact(run_dir: Path) -> Optional[RunPR]:
    artifact_path = run_dir / PUSH_PR_URL_FILE
    if not artifact_path.exists():
        return None
    try:
        raw = artifact_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not raw:
        return None
    for line in raw.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        pr = _run_pr_from_url(candidate)
        if pr is not None:
            return pr
    return None


def _extract_pr_from_user_response(user_response: Optional[str]) -> Optional[RunPR]:
    if user_response is None:
        return None
    for match in GITHUB_PR_PATTERN.finditer(user_response):
        pr = _run_pr_from_url(match.group(0))
        if pr is not None:
            return pr
    return None


def _extract_trailing_state_marker(output: str) -> Optional[RunState]:
    stripped_output = output.rstrip()
    if not stripped_output:
        return None
    last_line = stripped_output.splitlines()[-1].strip()
    if not last_line:
        return None
    match = TRAILING_STATE_MARKER_PATTERN.search(last_line)
    if match is None:
        return None
    state_value = match.group(1).strip().upper()
    if state_value not in STATE_MARKER_VALUES:
        return None
    return cast(RunState, state_value)


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
        ci_status=existing.ci_status or discovered.ci_status,
        ci_last_checked_at=existing.ci_last_checked_at or discovered.ci_last_checked_at,
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


def _extract_author_login(event: dict[str, Any]) -> str | None:
    author_payload = event.get("author")
    author_login = (
        author_payload.get("login")
        if isinstance(author_payload, dict)
        else None
    )
    if not isinstance(author_login, str):
        return None
    normalized = author_login.strip()
    if not normalized:
        return None
    return normalized


def _filter_events_by_review_actor_allowlist(
    events: Any,
    review_actor_usernames: tuple[str, ...],
) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(events, list):
        return [], 0
    if not review_actor_usernames:
        return [], len(events)
    if len(review_actor_usernames) == 1 and review_actor_usernames[0] == "*":
        filtered: list[dict[str, Any]] = []
        dropped = 0
        for event in events:
            if not isinstance(event, dict):
                dropped += 1
                continue
            author_login = _extract_author_login(event)
            if author_login is None:
                dropped += 1
                continue
            filtered.append(event)
        return filtered, dropped
    filtered: list[dict[str, Any]] = []
    dropped = 0
    for event in events:
        if not isinstance(event, dict):
            dropped += 1
            continue
        author_login = _extract_author_login(event)
        if author_login is None:
            dropped += 1
            continue
        if author_login.casefold() not in review_actor_usernames:
            dropped += 1
            continue
        filtered.append(event)
    return filtered, dropped


def _filter_review_payload_by_actor_allowlist(
    payload: dict[str, Any],
    review_actor_usernames: tuple[str, ...],
) -> tuple[dict[str, Any], int, int, int]:
    filtered_payload = dict(payload)
    filtered_comments, dropped_comments = _filter_events_by_review_actor_allowlist(
        payload.get("comments"),
        review_actor_usernames,
    )
    filtered_reviews, dropped_reviews = _filter_events_by_review_actor_allowlist(
        payload.get("reviews"),
        review_actor_usernames,
    )
    filtered_latest_reviews, dropped_latest_reviews = (
        _filter_events_by_review_actor_allowlist(
            payload.get("latestReviews"),
            review_actor_usernames,
        )
    )
    filtered_payload["comments"] = filtered_comments
    filtered_payload["reviews"] = filtered_reviews
    filtered_payload["latestReviews"] = filtered_latest_reviews
    return (
        filtered_payload,
        dropped_comments,
        dropped_reviews,
        dropped_latest_reviews,
    )


def _collect_review_states(reviews: Any) -> set[str]:
    if not isinstance(reviews, list) or not reviews:
        return set()
    states: set[str] = set()
    for review in reviews:
        if not isinstance(review, dict):
            continue
        state = str(review.get("state") or "").upper()
        if state:
            states.add(state)
    return states


def _latest_reviews_from_reviews(reviews: Any) -> list[dict[str, Any]]:
    if not isinstance(reviews, list) or not reviews:
        return []
    latest_by_author: dict[str, dict[str, Any]] = {}
    for review in reviews:
        if not isinstance(review, dict):
            continue
        author_login = _extract_author_login(review)
        submitted_at = review.get("submittedAt")
        if author_login is None or not isinstance(submitted_at, str):
            continue
        author_key = author_login.casefold()
        existing = latest_by_author.get(author_key)
        if existing is None:
            latest_by_author[author_key] = review
            continue
        existing_submitted_at = existing.get("submittedAt")
        if not isinstance(existing_submitted_at, str) or submitted_at > existing_submitted_at:
            latest_by_author[author_key] = review
    return list(latest_by_author.values())


def _review_events_for_status(payload: dict[str, Any]) -> list[dict[str, Any]]:
    latest_reviews = payload.get("latestReviews")
    if isinstance(latest_reviews, list) and latest_reviews:
        return [review for review in latest_reviews if isinstance(review, dict)]
    return _latest_reviews_from_reviews(payload.get("reviews"))


def _extract_latest_review_submitted_at_from_reviews(
    reviews: Any,
    target_state: str,
) -> Optional[str]:
    if not isinstance(reviews, list) or not reviews:
        return None
    normalized_target_state = target_state.upper()
    best_timestamp: Optional[str] = None
    for review in reviews:
        if not isinstance(review, dict):
            continue
        state = str(review.get("state", "")).upper()
        if state != normalized_target_state:
            continue
        submitted_at = review.get("submittedAt")
        if not isinstance(submitted_at, str):
            continue
        if best_timestamp is None or submitted_at > best_timestamp:
            best_timestamp = submitted_at
    return best_timestamp


def _derive_review_status_from_reviews(payload: dict[str, Any]) -> str:
    states = _collect_review_states(_review_events_for_status(payload))
    if "CHANGES_REQUESTED" in states:
        return "changes_requested"
    if "APPROVED" in states:
        return "approved"
    return "open"


def _ci_status_from_rollup(payload: dict[str, Any]) -> str:
    rollup = payload.get("statusCheckRollup")
    if not isinstance(rollup, list) or not rollup:
        return "pending"

    has_pending = False
    for item in rollup:
        if not isinstance(item, dict):
            has_pending = True
            continue
        state = str(item.get("state") or "").upper()
        status = str(item.get("status") or "").upper()
        conclusion_raw = item.get("conclusion")
        conclusion = (
            str(conclusion_raw).upper() if conclusion_raw is not None else ""
        )

        # StatusContext entries can expose state without status/conclusion.
        if state:
            if state in CI_PENDING_STATUSES:
                has_pending = True
                continue
            if state in CI_SUCCESS_CONCLUSIONS:
                continue
            if state in CI_FAILURE_CONCLUSIONS or state == "ERROR":
                return "failure"
            has_pending = True
            continue

        if status in CI_PENDING_STATUSES:
            has_pending = True
            continue
        if not conclusion and status != "COMPLETED":
            has_pending = True
            continue
        if conclusion and conclusion not in CI_SUCCESS_CONCLUSIONS:
            if conclusion in CI_FAILURE_CONCLUSIONS:
                return "failure"
            return "failure"

    if has_pending:
        return "pending"
    return "success"


def _load_comment_approval_settings(
    *,
    runtime_config: InnerLoopRuntimeConfig | None = None,
) -> CommentApprovalSettings:
    allowed_usernames = ()
    review_actor_usernames = ()
    pattern_text = DEFAULT_APPROVAL_COMMENT_PATTERN
    if runtime_config is not None:
        allowed_usernames = runtime_config.approval_comment_usernames
        review_actor_usernames = runtime_config.review_actor_usernames
        pattern_text = (
            runtime_config.approval_comment_pattern or DEFAULT_APPROVAL_COMMENT_PATTERN
        )
    used_default_pattern = False
    try:
        approval_regex = re.compile(pattern_text, re.IGNORECASE)
    except re.error:
        approval_regex = re.compile(DEFAULT_APPROVAL_COMMENT_PATTERN, re.IGNORECASE)
        pattern_text = DEFAULT_APPROVAL_COMMENT_PATTERN
        used_default_pattern = True
    return CommentApprovalSettings(
        allowed_usernames=allowed_usernames,
        review_actor_usernames=review_actor_usernames,
        pattern_text=pattern_text,
        approval_regex=approval_regex,
        used_default_pattern=used_default_pattern,
    )


def _extract_latest_allowlisted_approval_comment(
    payload: dict[str, Any],
    comment_approval: CommentApprovalSettings,
) -> ApprovalSignal | None:
    if not comment_approval.enabled:
        return None
    comments = payload.get("comments")
    if not isinstance(comments, list) or not comments:
        return None
    latest: ApprovalSignal | None = None
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        author_login = _extract_author_login(comment)
        if author_login is None:
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
        signal = ApprovalSignal(
            timestamp=comment_timestamp,
            author=author_login,
            source="comment",
            event_node_id=_extract_event_node_id(comment),
            viewer_has_thumbs_up_reaction=_viewer_has_thumbs_up_reaction(comment),
        )
        if latest is None or signal.timestamp > latest.timestamp:
            latest = signal
    return latest


def _extract_latest_allowlisted_approval_review(
    payload: dict[str, Any],
    comment_approval: CommentApprovalSettings,
) -> ApprovalSignal | None:
    if not comment_approval.enabled:
        return None
    reviews = payload.get("reviews")
    if not isinstance(reviews, list) or not reviews:
        return None
    latest: ApprovalSignal | None = None
    for review in reviews:
        if not isinstance(review, dict):
            continue
        review_state = str(review.get("state", "")).upper()
        if review_state not in APPROVAL_REVIEW_STATES:
            continue
        author_login = _extract_author_login(review)
        if author_login is None:
            continue
        if author_login.casefold() not in comment_approval.allowed_usernames:
            continue
        body = review.get("body")
        if not isinstance(body, str):
            continue
        if comment_approval.approval_regex.search(body) is None:
            continue
        submitted_at = review.get("submittedAt")
        if not isinstance(submitted_at, str):
            continue
        signal = ApprovalSignal(
            timestamp=submitted_at,
            author=author_login,
            source="review",
            event_node_id=_extract_event_node_id(review),
        )
        if latest is None or signal.timestamp > latest.timestamp:
            latest = signal
    return latest


def _extract_latest_plain_comment_feedback(
    payload: dict[str, Any],
) -> tuple[str, str] | None:
    comments = payload.get("comments")
    if not isinstance(comments, list) or not comments:
        return None
    latest: tuple[str, str] | None = None
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        author_login = _extract_author_login(comment)
        if author_login is None:
            continue
        created_at = comment.get("createdAt")
        updated_at = comment.get("updatedAt")
        comment_timestamp = updated_at if isinstance(updated_at, str) else created_at
        if not isinstance(comment_timestamp, str):
            continue
        if latest is None or comment_timestamp > latest[0]:
            latest = (comment_timestamp, author_login)
    return latest


def _extract_latest_commented_review_feedback(
    payload: dict[str, Any],
) -> tuple[str, str] | None:
    reviews = payload.get("reviews")
    if not isinstance(reviews, list) or not reviews:
        return None
    latest: tuple[str, str] | None = None
    for review in reviews:
        if not isinstance(review, dict):
            continue
        if str(review.get("state", "")).upper() != "COMMENTED":
            continue
        author_login = _extract_author_login(review)
        if author_login is None:
            continue
        submitted_at = review.get("submittedAt")
        if not isinstance(submitted_at, str):
            continue
        if latest is None or submitted_at > latest[0]:
            latest = (submitted_at, author_login)
    return latest


def _select_newer_feedback_signal(
    *,
    commented_review_feedback: tuple[str, str] | None,
    plain_comment_feedback: tuple[str, str] | None,
) -> tuple[str, str, str] | None:
    latest: tuple[str, str, str] | None = None
    if commented_review_feedback is not None:
        latest = (
            commented_review_feedback[0],
            commented_review_feedback[1],
            "commented_review",
        )
    if plain_comment_feedback is not None:
        plain_feedback = (
            plain_comment_feedback[0],
            plain_comment_feedback[1],
            "plain_comment",
        )
        if latest is None or plain_feedback[0] > latest[0]:
            latest = plain_feedback
    return latest


def _select_newer_approval_signal(
    *,
    approval_comment: ApprovalSignal | None,
    approval_review: ApprovalSignal | None,
) -> ApprovalSignal | None:
    latest: ApprovalSignal | None = None
    if approval_comment is not None:
        latest = approval_comment
    if approval_review is not None:
        if latest is None or approval_review.timestamp > latest.timestamp:
            latest = approval_review
    return latest


def _extract_event_node_id(event: dict[str, Any]) -> str | None:
    event_id = event.get("id")
    if not isinstance(event_id, str):
        return None
    normalized = event_id.strip()
    if not normalized:
        return None
    return normalized


def _viewer_has_thumbs_up_reaction(event: dict[str, Any]) -> bool:
    reaction_groups = event.get("reactionGroups")
    if not isinstance(reaction_groups, list):
        return False
    for group in reaction_groups:
        if not isinstance(group, dict):
            continue
        if str(group.get("content") or "").upper() != "THUMBS_UP":
            continue
        viewer_has_reacted = group.get("viewerHasReacted")
        if isinstance(viewer_has_reacted, bool):
            return viewer_has_reacted
    return False


def _react_to_approval_comment_with_thumbs_up(
    *,
    approval_signal: ApprovalSignal,
    pr_url: str,
    log_message: Optional[Callable[[str], None]] = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    def _log(message: str) -> None:
        if log_message is None:
            return
        log_message(message)

    if approval_signal.source != "comment":
        return
    if approval_signal.viewer_has_thumbs_up_reaction:
        _log(
            (
                "[loops] approval comment already has viewer thumbs-up reaction; "
                f"skipping reaction add: pr_url={pr_url} approver={approval_signal.author}"
            )
        )
        return
    if approval_signal.event_node_id is None:
        _log(
            (
                "[loops] approval comment is missing node id; cannot add reaction: "
                f"pr_url={pr_url} approver={approval_signal.author}"
            )
        )
        return

    result = subprocess.run(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={GH_ADD_REACTION_MUTATION}",
            "-f",
            f"subjectId={approval_signal.event_node_id}",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=os.environ.copy() if environ is None else dict(environ),
    )
    if result.returncode == 0:
        _log(
            (
                "[loops] added thumbs-up reaction to approval comment: "
                f"pr_url={pr_url} approver={approval_signal.author}"
            )
        )
        return

    error_output = (result.stderr.strip() or result.stdout.strip())
    normalized_error = error_output.casefold()
    if "reaction" in normalized_error and "already" in normalized_error:
        _log(
            (
                "[loops] thumbs-up reaction already exists on approval comment; "
                f"continuing: pr_url={pr_url} approver={approval_signal.author}"
            )
        )
        return
    _log(
        (
            "[loops] failed to add thumbs-up reaction to approval comment; "
            f"continuing without reaction: pr_url={pr_url} "
            f"approver={approval_signal.author} "
            f"stderr={error_output or '-'}"
        )
    )


def _should_resume_review_feedback(pr: RunPR) -> bool:
    if pr.review_status == "changes_requested":
        return _is_new_review(pr)
    if pr.review_status == "open" and pr.latest_review_submitted_at is not None:
        return _is_new_review(pr)
    return False


def _fetch_pr_status_with_gh_with_context(
    pr: RunPR,
    *,
    comment_approval: CommentApprovalSettings,
    log_message: Optional[Callable[[str], None]] = None,
    environ: Mapping[str, str] | None = None,
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
            f"approval_allowlisted_usernames={len(comment_approval.allowed_usernames)} "
            "review_actor_allowlisted_usernames="
            f"{len(comment_approval.review_actor_usernames)}"
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
        env=os.environ.copy() if environ is None else dict(environ),
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
    ci_status = _ci_status_from_rollup(payload)
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    review_decision_raw = payload.get("reviewDecision")
    latest_changes_requested_at: Optional[str]
    (
        effective_payload,
        dropped_comments,
        dropped_reviews,
        dropped_latest_reviews,
    ) = _filter_review_payload_by_actor_allowlist(
        payload,
        comment_approval.review_actor_usernames,
    )
    _log(
        (
            "[loops] applying review actor allowlist: "
            f"pr_url={pr.url} "
            f"allowlisted_usernames={len(comment_approval.review_actor_usernames)} "
            f"dropped_comments={dropped_comments} "
            f"dropped_reviews={dropped_reviews} "
            f"dropped_latest_reviews={dropped_latest_reviews}"
        )
    )
    wildcard_review_actor_allowlist = (
        len(comment_approval.review_actor_usernames) == 1
        and comment_approval.review_actor_usernames[0] == "*"
    )
    review_events = _review_events_for_status(effective_payload)
    review_status = _derive_review_status_from_reviews(effective_payload)
    latest_changes_requested_at = _extract_latest_review_submitted_at_from_reviews(
        review_events,
        "CHANGES_REQUESTED",
    )
    if latest_changes_requested_at is None and wildcard_review_actor_allowlist:
        latest_changes_requested_at = _extract_latest_review_submitted_at(
            payload,
            "CHANGES_REQUESTED",
        )
    if review_status == "open" and not review_events:
        if wildcard_review_actor_allowlist:
            review_status = _review_status_from_decision(review_decision_raw)
            latest_review_submitted_at = _extract_latest_review_submitted_at(
                payload, str(review_decision_raw or "")
            )
        else:
            latest_review_submitted_at = None
    else:
        review_state = (
            "APPROVED"
            if review_status == "approved"
            else "CHANGES_REQUESTED"
            if review_status == "changes_requested"
            else ""
        )
        if review_state:
            latest_review_submitted_at = _extract_latest_review_submitted_at_from_reviews(
                review_events,
                review_state,
            )
        else:
            latest_review_submitted_at = None
    approved_by_comment = False
    approved_by = ""
    if review_status != "approved":
        latest_approval_comment = _extract_latest_allowlisted_approval_comment(
            effective_payload,
            comment_approval,
        )
        latest_approval_review = _extract_latest_allowlisted_approval_review(
            effective_payload,
            comment_approval,
        )
        latest_approval_signal = _select_newer_approval_signal(
            approval_comment=latest_approval_comment,
            approval_review=latest_approval_review,
        )
        if latest_approval_signal is not None:
            approval_timestamp = latest_approval_signal.timestamp
            approval_author = latest_approval_signal.author
            approval_source = latest_approval_signal.source
            if (
                latest_changes_requested_at is None
                or approval_timestamp > latest_changes_requested_at
            ):
                review_status = "approved"
                approved_by_comment = True
                approved_by = approval_author
                latest_review_submitted_at = approval_timestamp
                _react_to_approval_comment_with_thumbs_up(
                    approval_signal=latest_approval_signal,
                    pr_url=pr.url,
                    log_message=_log,
                    environ=environ,
                )
            else:
                _log(
                    (
                        "[loops] ignoring allowlisted approval "
                        f"{approval_source} because a newer "
                        "changes_requested review exists: "
                        f"pr_url={pr.url} "
                        f"approval_signal_at={approval_timestamp} "
                        f"latest_changes_requested_at={latest_changes_requested_at}"
                    )
                )
        if review_status == "open":
            latest_commented_review = _extract_latest_commented_review_feedback(
                effective_payload
            )
            latest_plain_comment = _extract_latest_plain_comment_feedback(
                effective_payload
            )
            latest_feedback_signal = _select_newer_feedback_signal(
                commented_review_feedback=latest_commented_review,
                plain_comment_feedback=latest_plain_comment,
            )
            if latest_feedback_signal is not None:
                comment_timestamp, comment_author, feedback_type = (
                    latest_feedback_signal
                )
                latest_review_submitted_at = comment_timestamp
                if feedback_type == "commented_review":
                    _log(
                        (
                            "[loops] using latest COMMENTED review as feedback signal: "
                            f"pr_url={pr.url} "
                            f"comment_author={comment_author} "
                            f"comment_timestamp={comment_timestamp}"
                        )
                    )
                else:
                    _log(
                        (
                            "[loops] using latest plain PR comment as feedback signal: "
                            f"pr_url={pr.url} "
                            f"comment_author={comment_author} "
                            f"comment_timestamp={comment_timestamp}"
                        )
                    )

    updated_pr = RunPR(
        url=pr_url,
        number=parsed_number,
        repo=repo,
        review_status=review_status,
        ci_status=ci_status,
        ci_last_checked_at=now_iso,
        merged_at=merged_at_str,
        last_checked_at=now_iso,
        latest_review_submitted_at=latest_review_submitted_at,
        review_addressed_at=pr.review_addressed_at,
    )
    _log(
        (
            "[loops] PR status poll result: "
            f"pr_url={updated_pr.url} "
            f"review_decision={str(review_decision_raw or '').lower() or 'none'} "
            f"review_status={updated_pr.review_status or 'unknown'} "
            f"ci_status={updated_pr.ci_status or 'unknown'} "
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
        raw_result = handler(payload)
    except EOFError:
        append_log(run_log, "[loops] unable to read user input from stdin")
        return None
    except Exception as exc:
        append_log(run_log, f"[loops] user handoff handler failed: {exc}")
        return None
    try:
        handoff_result = _normalize_handoff_result(raw_result)
    except (TypeError, ValueError) as exc:
        append_log(run_log, f"[loops] user handoff handler returned invalid result: {exc}")
        return None
    if handoff_result.status == "waiting":
        append_log(run_log, "[loops] user handoff waiting for response")
        return None
    normalized = str(handoff_result.response or "").strip()
    if not normalized:
        append_log(run_log, "[loops] empty user response received")
        return None
    append_log(run_log, "[loops] user input received")
    return normalized


def _normalize_handoff_result(
    raw_result: HandoffResult | str | None,
) -> HandoffResult:
    if raw_result is None:
        return HandoffResult.waiting()
    if isinstance(raw_result, HandoffResult):
        return raw_result
    if isinstance(raw_result, str):
        stripped = raw_result.strip()
        if not stripped:
            return HandoffResult.waiting()
        return HandoffResult.from_response(stripped)
    raise TypeError(
        "expected HandoffResult, string, or null response from handoff handler"
    )


def _increment_idle_polls(
    *,
    run_json_path: Path,
    run_record: RunRecord,
    idle_polls: int,
    max_idle_polls: int,
    message: str,
    context: Optional[dict[str, Any]] = None,
    auto_approve_enabled: bool = False,
) -> tuple[RunRecord, int]:
    next_idle_polls = idle_polls + 1
    if next_idle_polls < max_idle_polls:
        return run_record, next_idle_polls
    return (
        _force_needs_input(
            run_json_path,
            run_record,
            message=message,
            context=context,
            auto_approve_enabled=auto_approve_enabled,
        ),
        0,
    )


def _force_needs_input(
    run_json_path: Path,
    run_record: RunRecord,
    *,
    message: str,
    context: Optional[dict[str, Any]] = None,
    auto_approve_enabled: bool = False,
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
        auto_approve_enabled=auto_approve_enabled,
    )


def _resolve_configured_handoff_handler_name(
    runtime_handoff_handler: str | None = None,
    *,
    allow_env_fallback: bool = True,
    environ: Mapping[str, str] | None = None,
) -> str:
    if runtime_handoff_handler is not None:
        raw_handler = runtime_handoff_handler
    elif allow_env_fallback:
        source = os.environ if environ is None else environ
        raw_handler = source.get("LOOPS_HANDOFF_HANDLER", DEFAULT_HANDOFF_HANDLER)
    else:
        raw_handler = DEFAULT_HANDOFF_HANDLER
    if not isinstance(raw_handler, str):
        raw_handler = DEFAULT_HANDOFF_HANDLER
    return validate_handoff_handler_name(raw_handler)


def _run_legacy_cli_entrypoint(argv: list[str] | None = None) -> None:
    """Run the canonical click-based inner-loop CLI from module entrypoint."""

    from loops.core.cli import main as cli_main

    args = sys.argv[1:] if argv is None else argv
    cli_main.main(
        args=["inner-loop", *args],
        prog_name="loops",
        standalone_mode=True,
    )


def main() -> None:
    _run_legacy_cli_entrypoint()


if __name__ == "__main__":
    main()
