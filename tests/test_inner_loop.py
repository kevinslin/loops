from __future__ import annotations

from dataclasses import replace
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from loops.state.approval_config import (
    DEFAULT_APPROVAL_COMMENT_PATTERN,
)
from loops.core.handoff_handlers import HandoffResult
from loops.state.inner_loop_runtime_config import (
    INNER_LOOP_RUNTIME_CONFIG_FILE,
    InnerLoopRuntimeConfig,
    read_inner_loop_runtime_config,
    write_inner_loop_runtime_config,
)
import loops.core.inner_loop as inner_loop_module
from loops.core.inner_loop import run_inner_loop
import loops.core.cli as cli_module
from loops.state.run_record import (
    RunAutoApprove,
    RunPR,
    RunRecord,
    Task,
    read_run_record,
    write_run_record,
)


def _task() -> Task:
    return Task(
        provider_id="github",
        id="4",
        title="Inner loop",
        status="ready",
        url="https://github.com/kevinslin/loops/issues/4",
        created_at="2026-02-09T00:00:00Z",
        updated_at="2026-02-09T00:00:00Z",
    )


def _write_run_record(
    run_dir: Path,
    *,
    pr: RunPR | None = None,
    codex_session: inner_loop_module.CodexSession | None = None,
    needs_user_input: bool = False,
    needs_user_input_payload: dict[str, object] | None = None,
    checkout_mode: str = "branch",
    starting_commit: str = "unknown",
) -> None:
    record = RunRecord(
        task=_task(),
        pr=pr,
        codex_session=codex_session,
        needs_user_input=needs_user_input,
        needs_user_input_payload=needs_user_input_payload,
        checkout_mode=checkout_mode,
        starting_commit=starting_commit,
        last_state="NEEDS_INPUT" if needs_user_input else "RUNNING",
        updated_at="",
    )
    write_run_record(run_dir / "run.json", record)


def _write_codex_stub(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import json",
                "import os",
                "import sys",
                "from pathlib import Path",
                "",
                "stdin = sys.stdin.read()",
                "prompt_log = os.environ.get('STUB_PROMPT_LOG')",
                "if prompt_log:",
                "    with Path(prompt_log).open('a', encoding='utf-8') as handle:",
                "        handle.write(stdin.replace('\\n', '\\\\n'))",
                "        handle.write('\\n')",
                "",
                "args_log = os.environ.get('STUB_ARGS_LOG')",
                "if args_log:",
                "    with Path(args_log).open('a', encoding='utf-8') as handle:",
                "        handle.write(' '.join(sys.argv[1:]))",
                "        handle.write('\\n')",
                "",
                "resume_session = None",
                "if 'resume' in sys.argv:",
                "    idx = sys.argv.index('resume')",
                "    if idx + 1 < len(sys.argv):",
                "        resume_session = sys.argv[idx + 1]",
                "",
                "counter_path = Path(os.environ['STUB_COUNTER_PATH'])",
                "if counter_path.exists():",
                "    count = int(counter_path.read_text())",
                "else:",
                "    count = 0",
                "count += 1",
                "counter_path.write_text(str(count))",
                "",
                "if resume_session is not None:",
                "    print(json.dumps({'session_id': resume_session}))",
                "else:",
                "    print(json.dumps({'session_id': f'session-{count}'}))",
                "if count == 1:",
                "    run_dir = os.environ.get('LOOPS_RUN_DIR')",
                "    if run_dir:",
                "        (Path(run_dir) / 'push-pr.url').write_text(",
                "            'https://github.com/acme/api/pull/42\\n',",
                "            encoding='utf-8',",
                "        )",
                "    print('Opened PR https://github.com/acme/api/pull/42')",
                "else:",
                "    print('cleanup complete')",
                "sys.exit(0)",
                "",
            ]
        )
    )


def _write_codex_cli_stub(path: Path) -> None:
    _write_codex_stub(path)
    path.chmod(0o755)

def _write_cleanup_stub(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "import os",
                "from pathlib import Path",
                "",
                "counter_path = Path(os.environ['STUB_COUNTER_PATH'])",
                "if counter_path.exists():",
                "    count = int(counter_path.read_text())",
                "else:",
                "    count = 0",
                "count += 1",
                "counter_path.write_text(str(count))",
                "print('cleanup complete')",
                "",
            ]
        )
    )


def _runtime_context(
    run_dir: Path,
    *,
    user_handoff_handler=None,
    pr_status_fetcher=None,
    sleep_fn=None,
    initial_poll_seconds: float = 5.0,
    max_poll_seconds: float = 60.0,
    max_idle_polls: int = 20,
    auto_approve_enabled: bool = False,
) -> inner_loop_module.InnerLoopRuntimeContext:
    if user_handoff_handler is None:
        user_handoff_handler = lambda _payload: HandoffResult.waiting()
    if pr_status_fetcher is None:
        pr_status_fetcher = lambda pr: pr
    if sleep_fn is None:
        sleep_fn = lambda _seconds: None
    return inner_loop_module.InnerLoopRuntimeContext(
        run_dir=run_dir,
        run_id=str(run_dir),
        run_json_path=run_dir / "run.json",
        run_log=run_dir / "run.log",
        agent_log=run_dir / "agent.log",
        environ=os.environ.copy(),
        command=["codex", "exec"],
        base_prompt=None,
        user_handoff_handler=user_handoff_handler,
        pr_status_fetcher=pr_status_fetcher,
        sleep_fn=sleep_fn,
        initial_poll_seconds=initial_poll_seconds,
        max_poll_seconds=max_poll_seconds,
        max_idle_polls=max_idle_polls,
        non_interactive_default_handoff=False,
        auto_approve_enabled=auto_approve_enabled,
    )


def test_handle_running_state_resets_control_and_consumes_user_response(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(run_dir)
    run_record = read_run_record(run_dir / "run.json")

    control = inner_loop_module.LoopControlState(
        backoff_seconds=17.0,
        idle_polls=3,
        next_user_response="keep this context",
        cleanup_executed_for_pr="https://github.com/acme/api/pull/7",
    )
    runtime = _runtime_context(run_dir)
    observed: dict[str, object] = {}

    def fake_run_codex_turn(**kwargs):
        observed.update(kwargs)
        return kwargs["run_record"]

    monkeypatch.setattr(inner_loop_module, "_run_codex_turn", fake_run_codex_turn)
    result = inner_loop_module._handle_running_state(
        run_record=run_record,
        runtime=runtime,
        control=control,
    )

    assert result.action == "codex_turn"
    assert observed["user_response"] == "keep this context"
    assert observed["review_feedback"] is False
    assert control.next_user_response is None
    assert control.cleanup_executed_for_pr is None
    assert control.backoff_seconds == runtime.initial_poll_seconds
    assert control.idle_polls == 0


def test_handle_needs_input_state_clears_payload_and_sets_next_response(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(
        run_dir,
        needs_user_input=True,
        needs_user_input_payload={"message": "Need decision"},
    )
    run_record = read_run_record(run_dir / "run.json")
    runtime = _runtime_context(
        run_dir,
        user_handoff_handler=lambda _payload: "  proceed with option A  ",
    )
    control = inner_loop_module.LoopControlState(backoff_seconds=23.0, idle_polls=5)

    result = inner_loop_module._handle_needs_input_state(
        run_record=run_record,
        runtime=runtime,
        control=control,
    )

    assert result.action == "needs_input_cleared"
    assert control.next_user_response == "proceed with option A"
    assert control.backoff_seconds == runtime.initial_poll_seconds
    assert control.idle_polls == 0
    persisted = read_run_record(run_dir / "run.json")
    assert persisted.needs_user_input is False
    assert persisted.needs_user_input_payload is None


def test_handle_waiting_on_review_state_missing_pr_forces_needs_input(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(run_dir)
    run_record = read_run_record(run_dir / "run.json")
    runtime = _runtime_context(run_dir)
    control = inner_loop_module.LoopControlState(backoff_seconds=9.0, idle_polls=2)

    result = inner_loop_module._handle_waiting_on_review_state(
        run_record=run_record,
        runtime=runtime,
        control=control,
    )

    assert result.action == "review_missing_pr"
    assert result.run_record.needs_user_input is True
    assert result.run_record.needs_user_input_payload == {
        "message": "Run is waiting on review but no PR metadata exists.",
    }


def test_handle_waiting_on_review_state_runs_auto_approve_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(
        run_dir,
        pr=RunPR(
            url="https://github.com/acme/api/pull/42",
            number=42,
            repo="acme/api",
            review_status="open",
            merged_at=None,
            last_checked_at="2026-02-09T00:00:00Z",
        ),
    )
    run_record = read_run_record(run_dir / "run.json")
    runtime = _runtime_context(
        run_dir,
        auto_approve_enabled=True,
        pr_status_fetcher=lambda pr: RunPR(
            url=pr.url,
            number=pr.number,
            repo=pr.repo,
            review_status="open",
            ci_status="success",
            merged_at=None,
            last_checked_at="2026-02-09T00:00:01Z",
        ),
    )
    control = inner_loop_module.LoopControlState(backoff_seconds=5.0)
    evaluations = {"count": 0}

    def fake_run_auto_approve_eval(*, run_record, runtime, control):
        evaluations["count"] += 1
        return write_run_record(
            runtime.run_json_path,
            replace(
                run_record,
                auto_approve=RunAutoApprove(
                    verdict="REJECT",
                    impact=2,
                    risk=4,
                    size=2,
                    judged_at="2026-02-09T00:00:02Z",
                    summary="Too risky to auto-merge.",
                ),
            ),
            auto_approve_enabled=runtime.auto_approve_enabled,
        )

    monkeypatch.setattr(inner_loop_module, "_run_auto_approve_eval", fake_run_auto_approve_eval)

    first = inner_loop_module._handle_waiting_on_review_state(
        run_record=run_record,
        runtime=runtime,
        control=control,
    )
    second = inner_loop_module._handle_waiting_on_review_state(
        run_record=first.run_record,
        runtime=runtime,
        control=control,
    )

    assert first.action == "auto_approve_eval"
    assert first.run_record.auto_approve is not None
    assert first.run_record.auto_approve.verdict == "REJECT"
    assert second.action == "review_poll"
    assert evaluations["count"] == 1


def test_handle_waiting_on_review_state_runs_auto_approve_for_changes_requested_without_new_feedback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    reviewed_at = "2026-02-09T00:00:01Z"
    _write_run_record(
        run_dir,
        pr=RunPR(
            url="https://github.com/acme/api/pull/42",
            number=42,
            repo="acme/api",
            review_status="changes_requested",
            merged_at=None,
            last_checked_at="2026-02-09T00:00:00Z",
            latest_review_submitted_at=reviewed_at,
            review_addressed_at=reviewed_at,
        ),
    )
    run_record = read_run_record(run_dir / "run.json")
    runtime = _runtime_context(
        run_dir,
        auto_approve_enabled=True,
        pr_status_fetcher=lambda pr: RunPR(
            url=pr.url,
            number=pr.number,
            repo=pr.repo,
            review_status="changes_requested",
            ci_status="success",
            merged_at=None,
            last_checked_at="2026-02-09T00:00:02Z",
            latest_review_submitted_at=reviewed_at,
            review_addressed_at=reviewed_at,
        ),
    )
    control = inner_loop_module.LoopControlState(backoff_seconds=5.0)
    evaluations = {"count": 0}

    def fake_run_auto_approve_eval(*, run_record, runtime, control):
        evaluations["count"] += 1
        return write_run_record(
            runtime.run_json_path,
            replace(
                run_record,
                auto_approve=RunAutoApprove(
                    verdict="REJECT",
                    impact=2,
                    risk=4,
                    size=2,
                    judged_at="2026-02-09T00:00:03Z",
                    summary="Too risky to auto-merge.",
                ),
            ),
            auto_approve_enabled=runtime.auto_approve_enabled,
        )

    monkeypatch.setattr(inner_loop_module, "_run_auto_approve_eval", fake_run_auto_approve_eval)

    result = inner_loop_module._handle_waiting_on_review_state(
        run_record=run_record,
        runtime=runtime,
        control=control,
    )

    assert result.action == "auto_approve_eval"
    assert evaluations["count"] == 1
    assert result.run_record.auto_approve is not None
    assert result.run_record.auto_approve.verdict == "REJECT"


def test_handle_waiting_on_review_state_skips_auto_approve_until_ci_green(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(
        run_dir,
        pr=RunPR(
            url="https://github.com/acme/api/pull/42",
            number=42,
            repo="acme/api",
            review_status="open",
            merged_at=None,
            last_checked_at="2026-02-09T00:00:00Z",
        ),
    )
    run_record = read_run_record(run_dir / "run.json")
    runtime = _runtime_context(
        run_dir,
        auto_approve_enabled=True,
        pr_status_fetcher=lambda pr: RunPR(
            url=pr.url,
            number=pr.number,
            repo=pr.repo,
            review_status="approved",
            ci_status="pending",
            merged_at=None,
            last_checked_at="2026-02-09T00:00:01Z",
        ),
    )
    control = inner_loop_module.LoopControlState(backoff_seconds=5.0)

    monkeypatch.setattr(
        inner_loop_module,
        "_run_auto_approve_eval",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("auto-approve should not run before CI is green")
        ),
    )

    result = inner_loop_module._handle_waiting_on_review_state(
        run_record=run_record,
        runtime=runtime,
        control=control,
    )

    assert result.action == "review_poll"
    assert result.run_record.auto_approve is None


def test_handle_pr_approved_state_runs_cleanup_once_per_pr_url(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(
        run_dir,
        pr=RunPR(
            url="https://github.com/acme/api/pull/42",
            number=42,
            repo="acme/api",
            review_status="approved",
            ci_status="success",
            merged_at=None,
            last_checked_at="2026-02-09T00:00:00Z",
        ),
    )
    run_record = read_run_record(run_dir / "run.json")
    cleanup_calls: list[str] = []

    def fake_invoke_codex(**kwargs):
        cleanup_calls.append(kwargs["prompt"])
        return "cleanup complete", 0, False

    monkeypatch.setattr(inner_loop_module, "_invoke_codex", fake_invoke_codex)

    def pr_status_fetcher(pr: RunPR) -> RunPR:
        return RunPR(
            url=pr.url,
            number=pr.number,
            repo=pr.repo,
            review_status="approved",
            ci_status="success",
            merged_at=None,
            last_checked_at="2026-02-09T00:00:05Z",
        )

    runtime = _runtime_context(run_dir, pr_status_fetcher=pr_status_fetcher)
    control = inner_loop_module.LoopControlState(backoff_seconds=5.0)

    first = inner_loop_module._handle_pr_approved_state(
        run_record=run_record,
        runtime=runtime,
        control=control,
    )
    second = inner_loop_module._handle_pr_approved_state(
        run_record=first.run_record,
        runtime=runtime,
        control=control,
    )

    assert first.action == "approved_poll"
    assert second.action == "approved_poll"
    assert len(cleanup_calls) == 1
    assert control.cleanup_executed_for_pr == "https://github.com/acme/api/pull/42"


def test_handle_state_rejects_unknown_state(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(run_dir)
    run_record = read_run_record(run_dir / "run.json")
    runtime = _runtime_context(run_dir)
    control = inner_loop_module.LoopControlState(backoff_seconds=5.0)

    with pytest.raises(ValueError, match="unsupported state"):
        inner_loop_module._handle_state(
            state="UNEXPECTED",
            run_record=run_record,
            runtime=runtime,
            control=control,
        )


def test_handle_pr_approved_state_sets_needs_input_when_cleanup_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(
        run_dir,
        pr=RunPR(
            url="https://github.com/acme/api/pull/42",
            number=42,
            repo="acme/api",
            review_status="approved",
            ci_status="success",
            merged_at=None,
            last_checked_at="2026-02-09T00:00:00Z",
        ),
    )
    run_record = read_run_record(run_dir / "run.json")

    def fake_invoke_codex(**_kwargs):
        return "cleanup failed", 17, False

    monkeypatch.setattr(inner_loop_module, "_invoke_codex", fake_invoke_codex)
    runtime = _runtime_context(run_dir)
    control = inner_loop_module.LoopControlState(backoff_seconds=5.0)

    result = inner_loop_module._handle_pr_approved_state(
        run_record=run_record,
        runtime=runtime,
        control=control,
    )

    assert result.action == "cleanup_failed"
    assert result.run_record.needs_user_input is True
    assert result.run_record.needs_user_input_payload == {
        "message": "Cleanup failed after PR approval. Please advise.",
        "context": {"exit_code": 17},
    }


def test_inner_loop_reaches_done_lifecycle(tmp_path, monkeypatch) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(run_dir)

    stub = tmp_path / "codex"
    _write_codex_cli_stub(stub)
    counter_path = tmp_path / "counter.txt"
    prompt_log_path = tmp_path / "prompts.log"
    args_log_path = tmp_path / "args.log"
    monkeypatch.setenv("STUB_COUNTER_PATH", str(counter_path))
    monkeypatch.setenv("STUB_PROMPT_LOG", str(prompt_log_path))
    monkeypatch.setenv("STUB_ARGS_LOG", str(args_log_path))
    monkeypatch.setenv(
        "CODEX_CMD",
        f"{shlex.quote(str(stub))} exec",
    )

    poll_calls = {"count": 0}

    def pr_status_fetcher(pr: RunPR) -> RunPR:
        poll_calls["count"] += 1
        if poll_calls["count"] == 1:
            return RunPR(
                url=pr.url,
                number=pr.number,
                repo=pr.repo,
                review_status="approved",
                ci_status="success",
                merged_at=None,
                last_checked_at="2026-02-09T00:00:01Z",
            )
        return RunPR(
            url=pr.url,
            number=pr.number,
            repo=pr.repo,
            review_status="approved",
            ci_status="success",
            merged_at="2026-02-09T00:00:02Z",
            last_checked_at="2026-02-09T00:00:02Z",
        )

    result = run_inner_loop(
        run_dir,
        pr_status_fetcher=pr_status_fetcher,
        sleep_fn=lambda _seconds: None,
        max_iterations=20,
    )

    assert result.last_state == "DONE"
    assert result.pr is not None
    assert result.pr.merged_at == "2026-02-09T00:00:02Z"
    assert result.codex_session is not None
    assert result.codex_session.id == "session-1"
    assert counter_path.read_text() == "2"  # RUNNING + PR_APPROVED cleanup
    run_log = (run_dir / "run.log").read_text()
    assert re.search(
        r"^\d{4}-\d{2}-\d{2}T[0-9:.+-]+ \[loops\] iteration 1 enter: state=RUNNING",
        run_log,
        re.MULTILINE,
    )
    assert "Opened PR https://github.com/acme/api/pull/42" in run_log
    assert "cleanup complete" in run_log
    assert "iteration 1 enter: state=RUNNING" in run_log
    assert "exit: next_state=DONE action=done_exit" in run_log
    prompts = prompt_log_path.read_text()
    assert "<state>RUNNING</state>" in prompts
    assert "<state>PR_APPROVED</state>" in prompts
    args = [line.strip() for line in args_log_path.read_text().splitlines() if line]
    assert args == ["exec", "exec resume session-1"]


def test_inner_loop_executes_task_status_hooks_for_running_and_done(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(run_dir)

    stub = tmp_path / "codex"
    _write_codex_cli_stub(stub)
    counter_path = tmp_path / "counter.txt"
    monkeypatch.setenv("STUB_COUNTER_PATH", str(counter_path))
    monkeypatch.setenv(
        "CODEX_CMD",
        f"{shlex.quote(str(stub))} exec",
    )

    class RecordingProvider:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def poll(self, limit: int | None = None):  # pragma: no cover - not used
            del limit
            return []

        def update_status(self, task_id: str, status: str) -> None:
            self.calls.append((task_id, status))

    provider = RecordingProvider()
    monkeypatch.setattr(
        inner_loop_module,
        "_resolve_task_provider_for_run",
        lambda **_kwargs: provider,
    )

    poll_calls = {"count": 0}

    def pr_status_fetcher(pr: RunPR) -> RunPR:
        poll_calls["count"] += 1
        if poll_calls["count"] == 1:
            return RunPR(
                url=pr.url,
                number=pr.number,
                repo=pr.repo,
                review_status="approved",
                ci_status="success",
                merged_at=None,
                last_checked_at="2026-02-09T00:00:01Z",
            )
        return RunPR(
            url=pr.url,
            number=pr.number,
            repo=pr.repo,
            review_status="approved",
            ci_status="success",
            merged_at="2026-02-09T00:00:02Z",
            last_checked_at="2026-02-09T00:00:02Z",
        )

    result = run_inner_loop(
        run_dir,
        pr_status_fetcher=pr_status_fetcher,
        sleep_fn=lambda _seconds: None,
        max_iterations=20,
    )

    assert result.last_state == "DONE"
    assert provider.calls == [("4", "IN_PROGRESS"), ("4", "DONE")]


def test_inner_loop_auto_approve_approve_verdict_allows_merge(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(
        run_dir,
        pr=RunPR(
            url="https://github.com/acme/api/pull/42",
            number=42,
            repo="acme/api",
            review_status="open",
            merged_at=None,
            last_checked_at="2026-02-09T00:00:00Z",
        ),
    )
    cleanup_stub = tmp_path / "cleanup_stub.py"
    _write_cleanup_stub(cleanup_stub)
    counter_path = tmp_path / "counter.txt"
    monkeypatch.setenv("STUB_COUNTER_PATH", str(counter_path))
    monkeypatch.setenv(
        "CODEX_CMD",
        f"{shlex.quote(sys.executable)} {shlex.quote(str(cleanup_stub))}",
    )
    monkeypatch.setenv("LOOPS_AUTO_APPROVE_ENABLED", "1")

    poll_calls = {"count": 0}

    def pr_status_fetcher(pr: RunPR) -> RunPR:
        poll_calls["count"] += 1
        if poll_calls["count"] == 1:
            return RunPR(
                url=pr.url,
                number=pr.number,
                repo=pr.repo,
                review_status="open",
                ci_status="success",
                merged_at=None,
                last_checked_at="2026-02-09T00:00:01Z",
            )
        return RunPR(
            url=pr.url,
            number=pr.number,
            repo=pr.repo,
            review_status="open",
            ci_status="success",
            merged_at="2026-02-09T00:00:02Z",
            last_checked_at="2026-02-09T00:00:02Z",
        )

    eval_calls = {"count": 0}

    def fake_run_auto_approve_eval(*, run_record, runtime, control):
        eval_calls["count"] += 1
        del control
        return write_run_record(
            runtime.run_json_path,
            replace(
                run_record,
                auto_approve=RunAutoApprove(
                    verdict="APPROVE",
                    impact=3,
                    risk=2,
                    size=2,
                    judged_at="2026-02-09T00:00:01Z",
                    summary="Auto-approve conditions satisfied.",
                ),
            ),
            auto_approve_enabled=runtime.auto_approve_enabled,
        )

    monkeypatch.setattr(inner_loop_module, "_run_auto_approve_eval", fake_run_auto_approve_eval)

    result = run_inner_loop(
        run_dir,
        pr_status_fetcher=pr_status_fetcher,
        sleep_fn=lambda _seconds: None,
        max_iterations=20,
    )

    assert result.last_state == "DONE"
    assert result.auto_approve is not None
    assert result.auto_approve.verdict == "APPROVE"
    assert eval_calls["count"] == 1
    assert counter_path.read_text() == "1"


def test_inner_loop_auto_approve_reject_blocks_without_rerun(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(
        run_dir,
        pr=RunPR(
            url="https://github.com/acme/api/pull/42",
            number=42,
            repo="acme/api",
            review_status="open",
            merged_at=None,
            last_checked_at="2026-02-09T00:00:00Z",
        ),
    )
    monkeypatch.setenv("LOOPS_AUTO_APPROVE_ENABLED", "1")

    def pr_status_fetcher(pr: RunPR) -> RunPR:
        return RunPR(
            url=pr.url,
            number=pr.number,
            repo=pr.repo,
            review_status="open",
            ci_status="success",
            merged_at=None,
            last_checked_at="2026-02-09T00:00:01Z",
        )

    eval_calls = {"count": 0}

    def fake_run_auto_approve_eval(*, run_record, runtime, control):
        eval_calls["count"] += 1
        del control
        return write_run_record(
            runtime.run_json_path,
            replace(
                run_record,
                auto_approve=RunAutoApprove(
                    verdict="REJECT",
                    impact=2,
                    risk=4,
                    size=2,
                    judged_at="2026-02-09T00:00:01Z",
                    summary="Risk remains too high for auto-merge.",
                ),
            ),
            auto_approve_enabled=runtime.auto_approve_enabled,
        )

    monkeypatch.setattr(inner_loop_module, "_run_auto_approve_eval", fake_run_auto_approve_eval)

    result = run_inner_loop(
        run_dir,
        pr_status_fetcher=pr_status_fetcher,
        user_handoff_handler=lambda _payload: HandoffResult.waiting(),
        sleep_fn=lambda _seconds: None,
        max_iterations=8,
        max_idle_polls=2,
    )

    assert result.last_state == "NEEDS_INPUT"
    assert result.auto_approve is not None
    assert result.auto_approve.verdict == "REJECT"
    assert eval_calls["count"] == 1


def test_inner_loop_escalates_when_approved_pr_never_merges(tmp_path, monkeypatch) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(
        run_dir,
        pr=RunPR(
            url="https://github.com/acme/api/pull/42",
            number=42,
            repo="acme/api",
            review_status="approved",
            ci_status="success",
            merged_at=None,
            last_checked_at="2026-02-09T00:00:00Z",
        ),
    )

    cleanup_stub = tmp_path / "cleanup_stub.py"
    _write_cleanup_stub(cleanup_stub)
    counter_path = tmp_path / "counter.txt"
    monkeypatch.setenv("STUB_COUNTER_PATH", str(counter_path))
    monkeypatch.setenv(
        "CODEX_CMD",
        f"{shlex.quote(sys.executable)} {shlex.quote(str(cleanup_stub))}",
    )

    def pr_status_fetcher(pr: RunPR) -> RunPR:
        return RunPR(
            url=pr.url,
            number=pr.number,
            repo=pr.repo,
            review_status="approved",
            ci_status="success",
            merged_at=None,
            last_checked_at="2026-02-09T00:00:01Z",
        )

    result = run_inner_loop(
        run_dir,
        pr_status_fetcher=pr_status_fetcher,
        sleep_fn=lambda _seconds: None,
        max_iterations=10,
        max_idle_polls=3,
    )

    assert result.last_state == "NEEDS_INPUT"
    assert result.needs_user_input is True
    assert result.needs_user_input_payload == {
        "message": (
            "PR is still approved but not merged after repeated polls. "
            "Please provide manual guidance."
        ),
    }
    assert counter_path.read_text() == "1"  # cleanup runs once per PR URL


def test_inner_loop_escalates_when_approved_merge_poll_errors(tmp_path, monkeypatch) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(
        run_dir,
        pr=RunPR(
            url="https://github.com/acme/api/pull/42",
            number=42,
            repo="acme/api",
            review_status="approved",
            ci_status="success",
            merged_at=None,
            last_checked_at="2026-02-09T00:00:00Z",
        ),
    )

    cleanup_stub = tmp_path / "cleanup_stub.py"
    _write_cleanup_stub(cleanup_stub)
    counter_path = tmp_path / "counter.txt"
    monkeypatch.setenv("STUB_COUNTER_PATH", str(counter_path))
    monkeypatch.setenv(
        "CODEX_CMD",
        f"{shlex.quote(sys.executable)} {shlex.quote(str(cleanup_stub))}",
    )

    def pr_status_fetcher(_pr: RunPR) -> RunPR:
        raise RuntimeError("gh unavailable")

    result = run_inner_loop(
        run_dir,
        pr_status_fetcher=pr_status_fetcher,
        sleep_fn=lambda _seconds: None,
        max_iterations=10,
        max_idle_polls=2,
    )

    assert result.last_state == "NEEDS_INPUT"
    assert result.needs_user_input is True
    assert result.needs_user_input_payload == {
        "message": (
            "Merge polling has been idle for too long. "
            "Please check merge status manually."
        ),
    }
    assert counter_path.read_text() == "1"  # cleanup runs once per PR URL
    run_log = (run_dir / "run.log").read_text()
    assert "failed to poll merge status: gh unavailable" in run_log


def test_inner_loop_sets_needs_user_input_on_nonzero_codex_exit(
    tmp_path, monkeypatch
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(run_dir)

    failing_stub = tmp_path / "codex_failing_stub.py"
    failing_stub.write_text(
        "\n".join(
            [
                "import json",
                "import sys",
                "print(json.dumps({'session_id': 'session-failed'}))",
                "print('codex simulated non-zero exit')",
                "sys.exit(17)",
                "",
            ]
        )
    )
    monkeypatch.setenv(
        "CODEX_CMD",
        f"{shlex.quote(sys.executable)} {shlex.quote(str(failing_stub))}",
    )

    result = run_inner_loop(
        run_dir,
        sleep_fn=lambda _seconds: None,
        max_iterations=5,
    )

    assert result.last_state == "NEEDS_INPUT"
    assert result.codex_session is not None
    assert result.codex_session.id == "session-failed"
    assert result.needs_user_input is True
    assert result.needs_user_input_payload == {
        "message": "Codex exited with a non-zero status. Provide guidance.",
        "context": {"exit_code": 17},
    }

    run_log = (run_dir / "run.log").read_text()
    assert "codex simulated non-zero exit" in run_log
    assert "[loops] codex exit code 17" in run_log


def test_inner_loop_uses_existing_needs_input_payload_and_user_response_in_prompt(
    tmp_path, monkeypatch
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(
        run_dir,
        needs_user_input=True,
        needs_user_input_payload={
            "message": "Need user decision",
            "context": {"scope": "priority"},
        },
    )

    stub = tmp_path / "codex_stub.py"
    _write_codex_stub(stub)
    counter_path = tmp_path / "counter.txt"
    prompt_log_path = tmp_path / "prompts.log"
    monkeypatch.setenv("STUB_COUNTER_PATH", str(counter_path))
    monkeypatch.setenv("STUB_PROMPT_LOG", str(prompt_log_path))
    monkeypatch.setenv(
        "CODEX_CMD",
        f"{shlex.quote(sys.executable)} {shlex.quote(str(stub))}",
    )

    def pr_status_fetcher(pr: RunPR) -> RunPR:
        return RunPR(
            url=pr.url,
            number=pr.number,
            repo=pr.repo,
            review_status="approved",
            merged_at="2026-02-09T00:00:09Z",
            last_checked_at="2026-02-09T00:00:09Z",
        )

    result = run_inner_loop(
        run_dir,
        pr_status_fetcher=pr_status_fetcher,
        user_handoff_handler=lambda payload: f"ack: {payload['message']}",
        sleep_fn=lambda _seconds: None,
        max_iterations=20,
    )

    assert result.last_state == "DONE"
    persisted = read_run_record(run_dir / "run.json")
    assert persisted.needs_user_input is False
    assert persisted.needs_user_input_payload is None

    prompts = prompt_log_path.read_text()
    assert "Implement the task and open a PR." in prompts
    assert "Wait only for review from the a-review subagent." in prompts
    assert (
        "NEVER wait for human PR review/comments inside the agent; the harness "
        "monitors review activity and will re-invoke you when feedback arrives."
        in prompts
    )
    assert (
        "When you run a-review, always post its response to the PR comments. If "
        "there are no findings, explicitly post that no issues were found."
        in prompts
    )
    assert "NEVER use the gen-notifier skill while running inside loops." in prompts
    assert (
        "Spawn the a-review subagent exactly once per conversation, only while state is "
        "<state>RUNNING</state>. Do not spawn a-review again in "
        "<state>WAITING_ON_REVIEW</state> or any later turn."
        in prompts
    )
    assert (
        "Do not update issue/project task status directly; Loops applies deterministic "
        "status transitions when states change."
        in prompts
    )
    assert "For the initial PR while state is <state>RUNNING</state>:" in prompts
    assert "if there are unstaged changes invoke:commit-code;" in prompts
    assert "and run python3 \"$REPO_ROOT/scripts/push-pr.py\" \"<pr-title>\" \"<pr-body-file>\";" in prompts
    assert "then invoke:check-ci and if CI fails invoke:fix-pr." in prompts
    assert "trigger:merge-pr when the state is exactly <state>PR_APPROVED</state>." in prompts
    assert "In the initial PR description, do not repeat the PR title in the body." in prompts
    assert "Include session context in the initial PR body using: sessionid: [session]" in prompts
    assert (
        "When posting PR progress comments, avoid duplicate messages by checking your latest "
        "PR comment before posting a new one."
        in prompts
    )
    assert (
        "Do not reuse stock opener text (for example: 'Addressed the new discussion feedback'); "
        "write a specific update for the current change or skip commenting when nothing changed."
        in prompts
    )
    assert (
        "If you need input from user, print what you need help with and end current "
        "conversation with <state>NEEDS_INPUT</>"
        in prompts
    )
    assert "Do not merge until the state is exactly <state>PR_APPROVED</state>." in prompts
    assert "User input:\\nack: Need user decision" in prompts
    assert "<state>RUNNING</state>" in prompts
    run_log = (run_dir / "run.log").read_text()
    assert re.search(
        r"\[loops\] user input for codex turn: present=True length=\d+",
        run_log,
    )


def test_inner_loop_includes_worktree_instruction_in_initial_prompt(
    tmp_path, monkeypatch
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(
        run_dir,
        checkout_mode="worktree",
        starting_commit="abc123",
    )

    stub = tmp_path / "codex_stub.py"
    _write_codex_stub(stub)
    counter_path = tmp_path / "counter.txt"
    prompt_log_path = tmp_path / "prompts.log"
    monkeypatch.setenv("STUB_COUNTER_PATH", str(counter_path))
    monkeypatch.setenv("STUB_PROMPT_LOG", str(prompt_log_path))
    monkeypatch.setenv(
        "CODEX_CMD",
        f"{shlex.quote(sys.executable)} {shlex.quote(str(stub))}",
    )

    def pr_status_fetcher(pr: RunPR) -> RunPR:
        return RunPR(
            url=pr.url,
            number=pr.number,
            repo=pr.repo,
            review_status="approved",
            merged_at="2026-02-09T00:00:09Z",
            last_checked_at="2026-02-09T00:00:09Z",
        )

    result = run_inner_loop(
        run_dir,
        pr_status_fetcher=pr_status_fetcher,
        sleep_fn=lambda _seconds: None,
        max_iterations=20,
    )

    assert result.last_state == "DONE"
    prompts = prompt_log_path.read_text()
    assert (
        "Before making code changes, create and switch to a new git worktree for this "
        "task and complete implementation from that worktree."
        in prompts
    )


def test_build_auto_approve_eval_prompt_includes_pr_comment_instruction() -> None:
    prompt = inner_loop_module._build_auto_approve_eval_prompt(
        "https://github.com/kevinslin/loops/issues/49",
        None,
        "https://github.com/acme/api/pull/42",
    )

    assert (
        "Post the ag-judge verdict and impact/risk/size scores to the PR comments."
        in prompt
    )
    assert (
        '{"verdict":"APPROVE|REJECT|ESCALATE","impact":1-5,"risk":1-5,"size":1-5,'
        '"summary":"..."}'
        in prompt
    )


def test_inner_loop_uses_user_response_for_review_feedback_turn(
    tmp_path, monkeypatch
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(
        run_dir,
        pr=RunPR(
            url="https://github.com/acme/api/pull/42",
            number=42,
            repo="acme/api",
            review_status="open",
            merged_at=None,
            last_checked_at="2026-02-09T00:00:00Z",
        ),
        needs_user_input=True,
        needs_user_input_payload={
            "message": "Need user decision",
            "context": {"scope": "review"},
        },
    )

    stub = tmp_path / "codex_stub.py"
    _write_codex_stub(stub)
    counter_path = tmp_path / "counter.txt"
    prompt_log_path = tmp_path / "prompts.log"
    monkeypatch.setenv("STUB_COUNTER_PATH", str(counter_path))
    monkeypatch.setenv("STUB_PROMPT_LOG", str(prompt_log_path))
    monkeypatch.setenv(
        "CODEX_CMD",
        f"{shlex.quote(sys.executable)} {shlex.quote(str(stub))}",
    )

    poll_calls = {"count": 0}

    def pr_status_fetcher(pr: RunPR) -> RunPR:
        poll_calls["count"] += 1
        if poll_calls["count"] == 1:
            return RunPR(
                url=pr.url,
                number=pr.number,
                repo=pr.repo,
                review_status="changes_requested",
                merged_at=None,
                last_checked_at="2026-02-09T00:00:01Z",
                latest_review_submitted_at="2026-02-09T00:00:01Z",
                review_addressed_at=pr.review_addressed_at,
            )
        return RunPR(
            url=pr.url,
            number=pr.number,
            repo=pr.repo,
            review_status="approved",
            merged_at="2026-02-09T00:00:02Z",
            last_checked_at="2026-02-09T00:00:02Z",
            review_addressed_at=pr.review_addressed_at,
        )

    result = run_inner_loop(
        run_dir,
        pr_status_fetcher=pr_status_fetcher,
        user_handoff_handler=lambda payload: f"ack: {payload['message']}",
        sleep_fn=lambda _seconds: None,
        max_iterations=30,
    )

    assert result.last_state == "DONE"
    assert counter_path.read_text() == "1"
    prompts = prompt_log_path.read_text()
    assert "User input:\\nack: Need user decision" in prompts
    assert "has changes requested. Address review feedback" in prompts
    assert "<state>WAITING_ON_REVIEW</state>" in prompts
    run_log = (run_dir / "run.log").read_text()
    assert re.search(
        r"\[loops\] user input for codex turn: present=True length=\d+",
        run_log,
    )


def test_inner_loop_resumes_from_waiting_on_review_without_codex(tmp_path, monkeypatch) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(
        run_dir,
        pr=RunPR(
            url="https://github.com/acme/api/pull/42",
            number=42,
            repo="acme/api",
            review_status="open",
            merged_at=None,
            last_checked_at="2026-02-09T00:00:00Z",
        ),
    )

    # This command should never execute in this test path.
    marker = tmp_path / "should_not_run.txt"
    failing_stub = tmp_path / "failing_stub.py"
    failing_stub.write_text(
        "\n".join(
            [
                "import sys",
                "from pathlib import Path",
                f"Path({str(marker)!r}).write_text('ran')",
                "sys.exit(1)",
                "",
            ]
        )
    )
    monkeypatch.setenv(
        "CODEX_CMD",
        f"{shlex.quote(sys.executable)} {shlex.quote(str(failing_stub))}",
    )

    def pr_status_fetcher(pr: RunPR) -> RunPR:
        return RunPR(
            url=pr.url,
            number=pr.number,
            repo=pr.repo,
            review_status="approved",
            merged_at="2026-02-09T00:00:22Z",
            last_checked_at="2026-02-09T00:00:22Z",
        )

    result = run_inner_loop(
        run_dir,
        pr_status_fetcher=pr_status_fetcher,
        sleep_fn=lambda _seconds: None,
        max_iterations=10,
    )

    assert result.last_state == "DONE"
    assert not marker.exists()


def test_inner_loop_resumes_codex_when_review_changes_requested(
    tmp_path, monkeypatch
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(
        run_dir,
        pr=RunPR(
            url="https://github.com/acme/api/pull/42",
            number=42,
            repo="acme/api",
            review_status="open",
            merged_at=None,
            last_checked_at="2026-02-09T00:00:00Z",
        ),
    )

    stub = tmp_path / "codex_stub.py"
    _write_codex_stub(stub)
    counter_path = tmp_path / "counter.txt"
    monkeypatch.setenv("STUB_COUNTER_PATH", str(counter_path))
    monkeypatch.setenv(
        "CODEX_CMD",
        f"{shlex.quote(sys.executable)} {shlex.quote(str(stub))}",
    )

    poll_calls = {"count": 0}

    def pr_status_fetcher(pr: RunPR) -> RunPR:
        poll_calls["count"] += 1
        if poll_calls["count"] == 1:
            return RunPR(
                url=pr.url,
                number=pr.number,
                repo=pr.repo,
                review_status="changes_requested",
                merged_at=None,
                last_checked_at="2026-02-09T00:00:01Z",
                latest_review_submitted_at="2026-02-09T00:00:01Z",
                review_addressed_at=pr.review_addressed_at,
            )
        return RunPR(
            url=pr.url,
            number=pr.number,
            repo=pr.repo,
            review_status="approved",
            merged_at="2026-02-09T00:00:02Z",
            last_checked_at="2026-02-09T00:00:02Z",
        )

    result = run_inner_loop(
        run_dir,
        pr_status_fetcher=pr_status_fetcher,
        sleep_fn=lambda _seconds: None,
        max_iterations=20,
    )

    assert result.last_state == "DONE"
    assert counter_path.read_text() == "1"  # resumed once to address feedback


def test_inner_loop_resumes_codex_when_new_plain_pr_comment_feedback(
    tmp_path, monkeypatch
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(
        run_dir,
        pr=RunPR(
            url="https://github.com/acme/api/pull/42",
            number=42,
            repo="acme/api",
            review_status="open",
            merged_at=None,
            last_checked_at="2026-02-09T00:00:00Z",
        ),
    )

    stub = tmp_path / "codex_stub.py"
    _write_codex_stub(stub)
    counter_path = tmp_path / "counter.txt"
    prompt_log_path = tmp_path / "prompts.log"
    monkeypatch.setenv("STUB_COUNTER_PATH", str(counter_path))
    monkeypatch.setenv("STUB_PROMPT_LOG", str(prompt_log_path))
    monkeypatch.setenv(
        "CODEX_CMD",
        f"{shlex.quote(sys.executable)} {shlex.quote(str(stub))}",
    )

    poll_calls = {"count": 0}
    comment_timestamp = "2026-02-09T00:00:01Z"

    def pr_status_fetcher(pr: RunPR) -> RunPR:
        poll_calls["count"] += 1
        if poll_calls["count"] == 1:
            return RunPR(
                url=pr.url,
                number=pr.number,
                repo=pr.repo,
                review_status="open",
                merged_at=None,
                last_checked_at="2026-02-09T00:00:01Z",
                latest_review_submitted_at=comment_timestamp,
                review_addressed_at=pr.review_addressed_at,
            )
        if poll_calls["count"] == 2:
            return RunPR(
                url=pr.url,
                number=pr.number,
                repo=pr.repo,
                review_status="open",
                merged_at=None,
                last_checked_at="2026-02-09T00:00:02Z",
                latest_review_submitted_at=comment_timestamp,
                review_addressed_at=pr.review_addressed_at,
            )
        return RunPR(
            url=pr.url,
            number=pr.number,
            repo=pr.repo,
            review_status="approved",
            merged_at="2026-02-09T00:00:03Z",
            last_checked_at="2026-02-09T00:00:03Z",
            review_addressed_at=pr.review_addressed_at,
        )

    result = run_inner_loop(
        run_dir,
        pr_status_fetcher=pr_status_fetcher,
        sleep_fn=lambda _seconds: None,
        max_iterations=25,
    )

    assert result.last_state == "DONE"
    assert counter_path.read_text() == "1"
    prompts = prompt_log_path.read_text()
    assert "has new discussion comments. Review the feedback" in prompts
    assert (
        "If there are no changes requested, summarize that and end the current turn."
        in prompts
    )
    assert "wait for comments" not in prompts
    run_log = (run_dir / "run.log").read_text()
    assert "new PR comment feedback detected; resuming codex" in run_log


def test_inner_loop_does_not_reinvoke_codex_for_same_review(
    tmp_path, monkeypatch
) -> None:
    """After Codex addresses a review, it must NOT be re-invoked if the
    reviewer hasn't submitted a new review (same submittedAt timestamp)."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(
        run_dir,
        pr=RunPR(
            url="https://github.com/acme/api/pull/42",
            number=42,
            repo="acme/api",
            review_status="open",
            merged_at=None,
            last_checked_at="2026-02-09T00:00:00Z",
        ),
    )

    stub = tmp_path / "codex_stub.py"
    _write_codex_stub(stub)
    counter_path = tmp_path / "counter.txt"
    monkeypatch.setenv("STUB_COUNTER_PATH", str(counter_path))
    monkeypatch.setenv(
        "CODEX_CMD",
        f"{shlex.quote(sys.executable)} {shlex.quote(str(stub))}",
    )

    poll_calls = {"count": 0}
    REVIEW_TIMESTAMP = "2026-02-09T00:00:01Z"

    def pr_status_fetcher(pr: RunPR) -> RunPR:
        poll_calls["count"] += 1
        if poll_calls["count"] <= 5:
            return RunPR(
                url=pr.url,
                number=pr.number,
                repo=pr.repo,
                review_status="changes_requested",
                merged_at=None,
                last_checked_at=f"2026-02-09T00:00:{poll_calls['count']:02d}Z",
                latest_review_submitted_at=REVIEW_TIMESTAMP,
                review_addressed_at=pr.review_addressed_at,
            )
        return RunPR(
            url=pr.url,
            number=pr.number,
            repo=pr.repo,
            review_status="approved",
            merged_at="2026-02-09T00:00:10Z",
            last_checked_at="2026-02-09T00:00:10Z",
        )

    result = run_inner_loop(
        run_dir,
        pr_status_fetcher=pr_status_fetcher,
        sleep_fn=lambda _seconds: None,
        max_iterations=30,
    )

    assert result.last_state == "DONE"
    assert counter_path.read_text() == "1"


def test_inner_loop_handles_multiple_review_rounds(
    tmp_path, monkeypatch
) -> None:
    """When a reviewer requests changes twice (two distinct review events),
    Codex should be invoked exactly twice."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(
        run_dir,
        pr=RunPR(
            url="https://github.com/acme/api/pull/42",
            number=42,
            repo="acme/api",
            review_status="open",
            merged_at=None,
            last_checked_at="2026-02-09T00:00:00Z",
        ),
    )

    stub = tmp_path / "codex_stub.py"
    _write_codex_stub(stub)
    counter_path = tmp_path / "counter.txt"
    monkeypatch.setenv("STUB_COUNTER_PATH", str(counter_path))
    monkeypatch.setenv(
        "CODEX_CMD",
        f"{shlex.quote(sys.executable)} {shlex.quote(str(stub))}",
    )

    poll_calls = {"count": 0}

    def pr_status_fetcher(pr: RunPR) -> RunPR:
        poll_calls["count"] += 1
        if poll_calls["count"] == 1:
            # First review round
            return RunPR(
                url=pr.url,
                number=pr.number,
                repo=pr.repo,
                review_status="changes_requested",
                merged_at=None,
                latest_review_submitted_at="2026-02-09T01:00:00Z",
                review_addressed_at=pr.review_addressed_at,
            )
        if poll_calls["count"] <= 3:
            # Same first review (reviewer hasn't re-reviewed)
            return RunPR(
                url=pr.url,
                number=pr.number,
                repo=pr.repo,
                review_status="changes_requested",
                merged_at=None,
                latest_review_submitted_at="2026-02-09T01:00:00Z",
                review_addressed_at=pr.review_addressed_at,
            )
        if poll_calls["count"] == 4:
            # Second review round with NEW timestamp
            return RunPR(
                url=pr.url,
                number=pr.number,
                repo=pr.repo,
                review_status="changes_requested",
                merged_at=None,
                latest_review_submitted_at="2026-02-09T02:00:00Z",
                review_addressed_at=pr.review_addressed_at,
            )
        # Approved and merged
        return RunPR(
            url=pr.url,
            number=pr.number,
            repo=pr.repo,
            review_status="approved",
            merged_at="2026-02-09T03:00:00Z",
            last_checked_at="2026-02-09T03:00:00Z",
        )

    result = run_inner_loop(
        run_dir,
        pr_status_fetcher=pr_status_fetcher,
        sleep_fn=lambda _seconds: None,
        max_iterations=30,
    )

    assert result.last_state == "DONE"
    assert counter_path.read_text() == "2"


def test_inner_loop_backward_compat_no_review_timestamp(
    tmp_path, monkeypatch
) -> None:
    """When pr_status_fetcher returns no latest_review_submitted_at
    (backward compat), the loop should still invoke Codex on
    changes_requested (conservative fallback)."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(
        run_dir,
        pr=RunPR(
            url="https://github.com/acme/api/pull/42",
            number=42,
            repo="acme/api",
            review_status="open",
            merged_at=None,
            last_checked_at="2026-02-09T00:00:00Z",
        ),
    )

    stub = tmp_path / "codex_stub.py"
    _write_codex_stub(stub)
    counter_path = tmp_path / "counter.txt"
    monkeypatch.setenv("STUB_COUNTER_PATH", str(counter_path))
    monkeypatch.setenv(
        "CODEX_CMD",
        f"{shlex.quote(sys.executable)} {shlex.quote(str(stub))}",
    )

    poll_calls = {"count": 0}

    def pr_status_fetcher(pr: RunPR) -> RunPR:
        poll_calls["count"] += 1
        if poll_calls["count"] == 1:
            return RunPR(
                url=pr.url,
                number=pr.number,
                repo=pr.repo,
                review_status="changes_requested",
                merged_at=None,
            )
        return RunPR(
            url=pr.url,
            number=pr.number,
            repo=pr.repo,
            review_status="approved",
            merged_at="2026-02-09T00:00:05Z",
            last_checked_at="2026-02-09T00:00:05Z",
        )

    result = run_inner_loop(
        run_dir,
        pr_status_fetcher=pr_status_fetcher,
        sleep_fn=lambda _seconds: None,
        max_iterations=20,
    )

    assert result.last_state == "DONE"
    assert counter_path.read_text() == "1"


def test_inner_loop_exits_promptly_when_needs_input_and_non_interactive(
    tmp_path, monkeypatch
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(
        run_dir,
        needs_user_input=True,
        needs_user_input_payload={"message": "Need manual input"},
    )

    monkeypatch.setattr(inner_loop_module, "_has_interactive_stdin", lambda: False)
    monkeypatch.setattr(
        inner_loop_module,
        "_default_user_handoff_handler",
        lambda _payload: (_ for _ in ()).throw(EOFError()),
    )

    sleep_calls: list[float] = []
    result = run_inner_loop(
        run_dir,
        sleep_fn=lambda seconds: sleep_calls.append(seconds),
        max_iterations=5,
    )

    assert result.last_state == "NEEDS_INPUT"
    assert sleep_calls == []
    log_output = (run_dir / "run.log").read_text()
    assert "non-interactive mode; exiting while waiting for user input" in log_output


def test_handle_needs_input_waiting_result(tmp_path: Path) -> None:
    run_log = tmp_path / "run.log"
    run_record = RunRecord(
        task=_task(),
        pr=None,
        codex_session=None,
        needs_user_input=True,
        needs_user_input_payload={"message": "Need manual decision"},
        last_state="NEEDS_INPUT",
        updated_at="",
    )

    response = inner_loop_module._handle_needs_input(
        run_record,
        handler=lambda _payload: HandoffResult.waiting(),
        run_log=run_log,
    )

    assert response is None
    assert "user handoff waiting for response" in run_log.read_text()


def test_handle_needs_input_response_result(tmp_path: Path) -> None:
    run_log = tmp_path / "run.log"
    run_record = RunRecord(
        task=_task(),
        pr=None,
        codex_session=None,
        needs_user_input=True,
        needs_user_input_payload={"message": "Need manual decision"},
        last_state="NEEDS_INPUT",
        updated_at="",
    )

    response = inner_loop_module._handle_needs_input(
        run_record,
        handler=lambda _payload: HandoffResult.from_response("  Proceed with plan A  "),
        run_log=run_log,
    )

    assert response == "Proceed with plan A"
    assert "user input received" in run_log.read_text()


def test_inner_loop_rejects_invalid_configured_handoff_handler(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(
        run_dir,
        needs_user_input=True,
        needs_user_input_payload={"message": "Need manual input"},
    )
    monkeypatch.setenv("LOOPS_HANDOFF_HANDLER", "unknown_handler")

    with pytest.raises(ValueError, match="handoff_handler"):
        run_inner_loop(
            run_dir,
            sleep_fn=lambda _seconds: None,
            max_iterations=1,
        )


def test_inner_loop_gh_comment_handler_requires_github_provider(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(
        run_dir,
        needs_user_input=True,
        needs_user_input_payload={"message": "Need manual input"},
    )
    monkeypatch.setenv("LOOPS_HANDOFF_HANDLER", "gh_comment_handler")

    with pytest.raises(ValueError, match="requires provider_id='github_projects_v2'"):
        run_inner_loop(
            run_dir,
            sleep_fn=lambda _seconds: None,
            max_iterations=1,
        )


def test_inner_loop_reads_handoff_handler_from_runtime_config(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(
        run_dir,
        needs_user_input=True,
        needs_user_input_payload={"message": "Need manual input"},
    )
    write_inner_loop_runtime_config(
        run_dir,
        InnerLoopRuntimeConfig(handoff_handler="gh_comment_handler"),
    )

    with pytest.raises(ValueError, match="requires provider_id='github_projects_v2'"):
        run_inner_loop(
            run_dir,
            sleep_fn=lambda _seconds: None,
            max_iterations=1,
        )


def test_inner_loop_runtime_config_omitted_runtime_keys_are_none(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / INNER_LOOP_RUNTIME_CONFIG_FILE).write_text("{}")

    runtime_config = read_inner_loop_runtime_config(run_dir)

    assert runtime_config is not None
    assert runtime_config.handoff_handler is None
    assert runtime_config.auto_approve_enabled is None
    assert runtime_config.stream_logs_stdout is None


def test_inner_loop_runtime_config_omitted_handoff_uses_env_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(
        run_dir,
        needs_user_input=True,
        needs_user_input_payload={"message": "Need manual input"},
    )
    (run_dir / INNER_LOOP_RUNTIME_CONFIG_FILE).write_text("{}")
    monkeypatch.setenv("LOOPS_HANDOFF_HANDLER", "gh_comment_handler")

    with pytest.raises(ValueError, match="requires provider_id='github_projects_v2'"):
        run_inner_loop(
            run_dir,
            sleep_fn=lambda _seconds: None,
            max_iterations=1,
        )


def test_inner_loop_runtime_config_codex_cmd_overrides_process_env(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(run_dir)

    stub = tmp_path / "codex"
    _write_codex_cli_stub(stub)
    counter_path = tmp_path / "counter.txt"
    prompt_log_path = tmp_path / "prompts.log"
    args_log_path = tmp_path / "args.log"
    write_inner_loop_runtime_config(
        run_dir,
        InnerLoopRuntimeConfig(
            env={
                "CODEX_CMD": f"{shlex.quote(str(stub))} exec",
                "STUB_COUNTER_PATH": str(counter_path),
                "STUB_PROMPT_LOG": str(prompt_log_path),
                "STUB_ARGS_LOG": str(args_log_path),
            },
        ),
    )
    monkeypatch.setenv(
        "CODEX_CMD",
        f"{shlex.quote(sys.executable)} -c \"import sys; sys.exit(13)\"",
    )

    poll_calls = {"count": 0}

    def pr_status_fetcher(pr: RunPR) -> RunPR:
        poll_calls["count"] += 1
        if poll_calls["count"] == 1:
            return RunPR(
                url=pr.url,
                number=pr.number,
                repo=pr.repo,
                review_status="approved",
                ci_status="success",
                merged_at=None,
                last_checked_at="2026-02-09T00:00:01Z",
            )
        return RunPR(
            url=pr.url,
            number=pr.number,
            repo=pr.repo,
            review_status="approved",
            ci_status="success",
            merged_at="2026-02-09T00:00:02Z",
            last_checked_at="2026-02-09T00:00:02Z",
        )

    result = run_inner_loop(
        run_dir,
        pr_status_fetcher=pr_status_fetcher,
        sleep_fn=lambda _seconds: None,
        max_iterations=20,
    )

    assert result.last_state == "DONE"
    assert result.pr is not None
    assert result.pr.merged_at == "2026-02-09T00:00:02Z"
    assert counter_path.read_text() == "2"
    args = [line.strip() for line in args_log_path.read_text().splitlines() if line]
    assert args == ["exec", "exec resume session-1"]


def test_inner_loop_overrides_stale_loops_run_dir_env_for_codex_turn(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "active-run"
    run_dir.mkdir()
    stale_run_dir = tmp_path / "stale-run"
    stale_run_dir.mkdir()
    _write_run_record(run_dir)

    stub = tmp_path / "codex"
    _write_codex_cli_stub(stub)
    counter_path = tmp_path / "counter.txt"
    prompt_log_path = tmp_path / "prompts.log"
    args_log_path = tmp_path / "args.log"
    monkeypatch.setenv("STUB_COUNTER_PATH", str(counter_path))
    monkeypatch.setenv("STUB_PROMPT_LOG", str(prompt_log_path))
    monkeypatch.setenv("STUB_ARGS_LOG", str(args_log_path))
    monkeypatch.setenv(
        "CODEX_CMD",
        f"{shlex.quote(str(stub))} exec",
    )
    monkeypatch.setenv("LOOPS_RUN_DIR", str(stale_run_dir))

    poll_calls = {"count": 0}

    def pr_status_fetcher(pr: RunPR) -> RunPR:
        poll_calls["count"] += 1
        if poll_calls["count"] == 1:
            return RunPR(
                url=pr.url,
                number=pr.number,
                repo=pr.repo,
                review_status="approved",
                ci_status="success",
                merged_at=None,
                last_checked_at="2026-02-09T00:00:01Z",
            )
        return RunPR(
            url=pr.url,
            number=pr.number,
            repo=pr.repo,
            review_status="approved",
            ci_status="success",
            merged_at="2026-02-09T00:00:02Z",
            last_checked_at="2026-02-09T00:00:02Z",
        )

    result = run_inner_loop(
        run_dir,
        pr_status_fetcher=pr_status_fetcher,
        sleep_fn=lambda _seconds: None,
        max_iterations=20,
    )

    assert result.last_state == "DONE"
    assert (run_dir / inner_loop_module.PUSH_PR_URL_FILE).exists()
    assert not (stale_run_dir / inner_loop_module.PUSH_PR_URL_FILE).exists()


def test_inner_loop_raises_on_malformed_runtime_config(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(run_dir)
    (run_dir / INNER_LOOP_RUNTIME_CONFIG_FILE).write_text("{invalid-json")

    with pytest.raises(json.JSONDecodeError):
        run_inner_loop(
            run_dir,
            sleep_fn=lambda _seconds: None,
            max_iterations=1,
        )

    assert "failed to load run runtime config; aborting" in (run_dir / "run.log").read_text()


def test_inner_loop_runtime_config_log_streaming_does_not_mutate_process_env(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(run_dir)
    write_inner_loop_runtime_config(
        run_dir,
        InnerLoopRuntimeConfig(stream_logs_stdout=False),
    )
    monkeypatch.setenv("LOOPS_STREAM_LOGS_STDOUT", "1")
    monkeypatch.setattr(
        inner_loop_module,
        "_run_codex_turn",
        lambda **kwargs: kwargs["run_record"],
    )

    run_inner_loop(
        run_dir,
        sleep_fn=lambda _seconds: None,
        max_iterations=1,
    )

    assert os.environ.get("LOOPS_STREAM_LOGS_STDOUT") == "1"
    assert read_run_record(run_dir / "run.json").stream_logs_stdout is False


def test_inner_loop_runtime_config_omitted_stream_logs_uses_env_fallback(
    tmp_path: Path,
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(run_dir)
    (run_dir / INNER_LOOP_RUNTIME_CONFIG_FILE).write_text("{}")
    monkeypatch.setenv("LOOPS_STREAM_LOGS_STDOUT", "1")
    monkeypatch.setattr(
        inner_loop_module,
        "_run_codex_turn",
        lambda **kwargs: kwargs["run_record"],
    )

    run_inner_loop(
        run_dir,
        sleep_fn=lambda _seconds: None,
        max_iterations=1,
    )

    captured = capsys.readouterr()
    assert "[loops] iteration 1 enter: state=RUNNING" in captured.out
    assert read_run_record(run_dir / "run.json").stream_logs_stdout is True


def test_inner_loop_runtime_config_omitted_auto_approve_uses_env_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(
        run_dir,
        pr=RunPR(
            url="https://github.com/acme/api/pull/42",
            number=42,
            repo="acme/api",
            review_status="open",
            merged_at=None,
            last_checked_at="2026-02-09T00:00:00Z",
        ),
    )
    (run_dir / INNER_LOOP_RUNTIME_CONFIG_FILE).write_text("{}")
    monkeypatch.setenv("LOOPS_AUTO_APPROVE_ENABLED", "1")

    def pr_status_fetcher(pr: RunPR) -> RunPR:
        return RunPR(
            url=pr.url,
            number=pr.number,
            repo=pr.repo,
            review_status="open",
            ci_status="success",
            merged_at=None,
            last_checked_at="2026-02-09T00:00:01Z",
        )

    eval_calls = {"count": 0}

    def fake_run_auto_approve_eval(*, run_record, runtime, control):
        eval_calls["count"] += 1
        del control
        return write_run_record(
            runtime.run_json_path,
            replace(
                run_record,
                auto_approve=RunAutoApprove(
                    verdict="REJECT",
                    impact=2,
                    risk=4,
                    size=2,
                    judged_at="2026-02-09T00:00:01Z",
                    summary="Risk remains too high for auto-merge.",
                ),
            ),
            auto_approve_enabled=runtime.auto_approve_enabled,
        )

    monkeypatch.setattr(inner_loop_module, "_run_auto_approve_eval", fake_run_auto_approve_eval)

    result = run_inner_loop(
        run_dir,
        pr_status_fetcher=pr_status_fetcher,
        user_handoff_handler=lambda _payload: HandoffResult.waiting(),
        sleep_fn=lambda _seconds: None,
        max_iterations=8,
        max_idle_polls=2,
    )

    assert result.last_state == "NEEDS_INPUT"
    assert result.auto_approve is not None
    assert result.auto_approve.verdict == "REJECT"
    assert eval_calls["count"] == 1


def test_inner_loop_runtime_config_keeps_process_env_codex_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(run_dir)

    stub = tmp_path / "codex"
    _write_codex_cli_stub(stub)
    counter_path = tmp_path / "counter.txt"
    prompt_log_path = tmp_path / "prompts.log"
    args_log_path = tmp_path / "args.log"
    write_inner_loop_runtime_config(
        run_dir,
        InnerLoopRuntimeConfig(
            env={
                "STUB_COUNTER_PATH": str(counter_path),
                "STUB_PROMPT_LOG": str(prompt_log_path),
                "STUB_ARGS_LOG": str(args_log_path),
            },
        ),
    )
    monkeypatch.setenv(
        "CODEX_CMD",
        f"{shlex.quote(str(stub))} exec",
    )

    poll_calls = {"count": 0}

    def pr_status_fetcher(pr: RunPR) -> RunPR:
        poll_calls["count"] += 1
        if poll_calls["count"] == 1:
            return RunPR(
                url=pr.url,
                number=pr.number,
                repo=pr.repo,
                review_status="approved",
                ci_status="success",
                merged_at=None,
                last_checked_at="2026-02-09T00:00:01Z",
            )
        return RunPR(
            url=pr.url,
            number=pr.number,
            repo=pr.repo,
            review_status="approved",
            ci_status="success",
            merged_at="2026-02-09T00:00:02Z",
            last_checked_at="2026-02-09T00:00:02Z",
        )

    result = run_inner_loop(
        run_dir,
        pr_status_fetcher=pr_status_fetcher,
        sleep_fn=lambda _seconds: None,
        max_iterations=20,
    )

    assert result.last_state == "DONE"
    assert result.pr is not None
    assert result.pr.merged_at == "2026-02-09T00:00:02Z"
    assert counter_path.read_text() == "2"
    args = [line.strip() for line in args_log_path.read_text().splitlines() if line]
    assert args == ["exec", "exec resume session-1"]


def test_build_codex_turn_command_adds_resume_for_codex_exec() -> None:
    session = inner_loop_module.CodexSession(id="session-123")
    command, strategy = inner_loop_module._build_codex_turn_command(
        ["codex", "exec", "--json"],
        codex_session=session,
    )

    assert strategy == "resume"
    assert command == ["codex", "exec", "--json", "resume", "session-123"]


def test_build_codex_turn_command_keeps_non_codex_base_command() -> None:
    session = inner_loop_module.CodexSession(id="session-123")
    base_command = [sys.executable, "codex_stub.py"]
    command, strategy = inner_loop_module._build_codex_turn_command(
        base_command,
        codex_session=session,
    )

    assert strategy == "resume_unsupported"
    assert command == base_command


def test_invoke_codex_retries_without_resume_on_resume_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run_codex(
        command: list[str],
        _prompt: str,
        _agent_log: Path,
        *,
        environ: dict[str, str],
    ) -> tuple[str, int]:
        del environ
        calls.append(command)
        if len(calls) == 1:
            return "resume failed", 17
        return (
            json.dumps({"session_id": "session-fresh"})
            + "\nOpened PR https://github.com/acme/api/pull/99\n",
            0,
        )

    monkeypatch.setattr(inner_loop_module, "_run_codex", fake_run_codex)
    output, exit_code, resume_fallback_used = inner_loop_module._invoke_codex(
        base_command=["codex", "exec"],
        prompt="prompt",
        agent_log=tmp_path / "agent.log",
        run_log=tmp_path / "run.log",
        codex_session=inner_loop_module.CodexSession(id="stale-session"),
        turn_label="codex turn",
        environ=os.environ.copy(),
    )

    assert resume_fallback_used is True
    assert exit_code == 0
    assert calls == [
        ["codex", "exec", "resume", "stale-session"],
        ["codex", "exec"],
    ]
    assert inner_loop_module._extract_session_id(output) == "session-fresh"


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("work complete\n<state>NEEDS_INPUT</state>\n", "NEEDS_INPUT"),
        ("work complete\n<state>needs_input</>\n", "NEEDS_INPUT"),
        ("work complete\n   <state>WAITING_ON_REVIEW</state>   \n", "WAITING_ON_REVIEW"),
        ("work complete\n<state>NEEDS_INPUT</state>\nfollow up\n", None),
        ("work complete\nstatus: <state>WAITING_ON_REVIEW</state>\n", None),
        ("work complete\n<state>RUNNING</state> <state>NEEDS_INPUT</state>\n", None),
        ("work complete\n<state>UNKNOWN</state>\n", None),
    ],
)
def test_extract_trailing_state_marker(output: str, expected: str | None) -> None:
    assert inner_loop_module._extract_trailing_state_marker(output) == expected


def test_run_codex_turn_sets_needs_input_from_trailing_state_marker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(
        run_dir,
        pr=RunPR(
            url="https://github.com/acme/api/pull/42",
            number=42,
            repo="acme/api",
            review_status="open",
        ),
    )

    def fake_invoke_codex(
        *,
        base_command: list[str],
        prompt: str,
        agent_log: Path,
        run_log: Path,
        codex_session: inner_loop_module.CodexSession | None,
        turn_label: str,
        environ: dict[str, str],
    ) -> tuple[str, int, bool]:
        del base_command, prompt, agent_log, run_log, codex_session, turn_label, environ
        return (
            json.dumps({"session_id": "session-2"})
            + "\nOpened PR https://github.com/acme/api/pull/42\n<state>NEEDS_INPUT</state>\n",
            0,
            False,
        )

    monkeypatch.setattr(inner_loop_module, "_invoke_codex", fake_invoke_codex)
    run_json_path = run_dir / "run.json"
    updated = inner_loop_module._run_codex_turn(
        run_json_path=run_json_path,
        run_log=run_dir / "run.log",
        agent_log=run_dir / "agent.log",
        run_record=read_run_record(run_json_path),
        command=["codex", "exec"],
        environ=os.environ.copy(),
        base_prompt=None,
        review_feedback=False,
    )

    assert updated.needs_user_input is True
    assert updated.needs_user_input_payload == {
        "message": "Codex requested user input via trailing state marker. Provide guidance."
    }
    assert updated.pr is not None
    assert updated.pr.url == "https://github.com/acme/api/pull/42"
    assert updated.codex_session is not None
    assert updated.codex_session.id == "session-2"
    assert "codex requested state via marker: NEEDS_INPUT" in (run_dir / "run.log").read_text()


def test_run_codex_turn_discovers_initial_pr_from_push_pr_artifact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(run_dir)
    (run_dir / inner_loop_module.PUSH_PR_URL_FILE).write_text(
        "https://github.com/acme/api/pull/84\n",
        encoding="utf-8",
    )

    def fake_invoke_codex(
        *,
        base_command: list[str],
        prompt: str,
        agent_log: Path,
        run_log: Path,
        codex_session: inner_loop_module.CodexSession | None,
        turn_label: str,
        environ: dict[str, str],
    ) -> tuple[str, int, bool]:
        del base_command, prompt, agent_log, run_log, codex_session, turn_label, environ
        return (json.dumps({"session_id": "session-2"}) + "\nrun complete\n", 0, False)

    monkeypatch.setattr(inner_loop_module, "_invoke_codex", fake_invoke_codex)
    run_json_path = run_dir / "run.json"
    updated = inner_loop_module._run_codex_turn(
        run_json_path=run_json_path,
        run_log=run_dir / "run.log",
        agent_log=run_dir / "agent.log",
        run_record=read_run_record(run_json_path),
        command=["codex", "exec"],
        environ=os.environ.copy(),
        base_prompt=None,
        review_feedback=False,
    )

    assert updated.pr is not None
    assert updated.pr.url == "https://github.com/acme/api/pull/84"
    assert updated.needs_user_input is False
    assert updated.needs_user_input_payload is None


def test_run_codex_turn_does_not_fallback_to_stdout_for_initial_pr_discovery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(run_dir)

    def fake_invoke_codex(
        *,
        base_command: list[str],
        prompt: str,
        agent_log: Path,
        run_log: Path,
        codex_session: inner_loop_module.CodexSession | None,
        turn_label: str,
        environ: dict[str, str],
    ) -> tuple[str, int, bool]:
        del base_command, prompt, agent_log, run_log, codex_session, turn_label, environ
        return (
            json.dumps({"session_id": "session-2"})
            + "\nOpened PR https://github.com/acme/api/pull/42\n",
            0,
            False,
        )

    monkeypatch.setattr(inner_loop_module, "_invoke_codex", fake_invoke_codex)
    run_json_path = run_dir / "run.json"
    updated = inner_loop_module._run_codex_turn(
        run_json_path=run_json_path,
        run_log=run_dir / "run.log",
        agent_log=run_dir / "agent.log",
        run_record=read_run_record(run_json_path),
        command=["codex", "exec"],
        environ=os.environ.copy(),
        base_prompt=None,
        review_feedback=False,
    )

    assert updated.pr is None
    assert updated.needs_user_input is True
    assert updated.needs_user_input_payload == {
        "message": (
            "Loops could not determine a PR URL from push-pr.py artifact output. "
            "Provide the PR URL or rerun push-pr.py."
        ),
        "context": {"artifact_path": str(run_dir / inner_loop_module.PUSH_PR_URL_FILE)},
    }
    run_log = (run_dir / "run.log").read_text()
    assert "deterministic PR discovery failed" in run_log


def test_run_codex_turn_recovers_initial_pr_from_user_input_when_artifact_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(run_dir)

    def fake_invoke_codex(
        *,
        base_command: list[str],
        prompt: str,
        agent_log: Path,
        run_log: Path,
        codex_session: inner_loop_module.CodexSession | None,
        turn_label: str,
        environ: dict[str, str],
    ) -> tuple[str, int, bool]:
        del base_command, prompt, agent_log, run_log, codex_session, turn_label, environ
        return (json.dumps({"session_id": "session-2"}) + "\nrun complete\n", 0, False)

    monkeypatch.setattr(inner_loop_module, "_invoke_codex", fake_invoke_codex)
    run_json_path = run_dir / "run.json"
    updated = inner_loop_module._run_codex_turn(
        run_json_path=run_json_path,
        run_log=run_dir / "run.log",
        agent_log=run_dir / "agent.log",
        run_record=read_run_record(run_json_path),
        command=["codex", "exec"],
        environ=os.environ.copy(),
        base_prompt=None,
        user_response="Here is the PR: https://github.com/acme/api/pull/93",
        review_feedback=False,
    )

    assert updated.pr is not None
    assert updated.pr.url == "https://github.com/acme/api/pull/93"
    assert updated.needs_user_input is False
    assert updated.needs_user_input_payload is None
    assert (
        "deterministic PR discovery recovered from user input PR URL"
        in (run_dir / "run.log").read_text()
    )


def test_extract_pr_from_push_pr_artifact_returns_none_when_unreadable(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / inner_loop_module.PUSH_PR_URL_FILE).mkdir()

    assert inner_loop_module._extract_pr_from_push_pr_artifact(run_dir) is None


def test_run_codex_turn_clears_stale_session_after_failed_resume_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(
        run_dir,
        codex_session=inner_loop_module.CodexSession(id="stale-session"),
    )

    def fake_invoke_codex(
        *,
        base_command: list[str],
        prompt: str,
        agent_log: Path,
        run_log: Path,
        codex_session: inner_loop_module.CodexSession | None,
        turn_label: str,
        environ: dict[str, str],
    ) -> tuple[str, int, bool]:
        del base_command, prompt, agent_log, run_log, codex_session, turn_label, environ
        return "resume failed\nfallback failed\n", 17, True

    monkeypatch.setattr(inner_loop_module, "_invoke_codex", fake_invoke_codex)
    run_json_path = run_dir / "run.json"
    updated = inner_loop_module._run_codex_turn(
        run_json_path=run_json_path,
        run_log=run_dir / "run.log",
        agent_log=run_dir / "agent.log",
        run_record=read_run_record(run_json_path),
        command=["codex", "exec"],
        environ=os.environ.copy(),
        base_prompt=None,
        review_feedback=False,
    )

    assert updated.codex_session is None
    assert updated.needs_user_input is True
    assert updated.needs_user_input_payload == {
        "message": "Codex exited with a non-zero status. Provide guidance.",
        "context": {"exit_code": 17},
    }


def test_run_codex_streams_output_to_agent_log_while_running(tmp_path) -> None:
    agent_log = tmp_path / "agent.log"
    stub = tmp_path / "stream_stub.py"
    stub.write_text(
        "\n".join(
            [
                "import time",
                "print('first line', flush=True)",
                "time.sleep(0.3)",
                "print('second line', flush=True)",
            ]
        )
    )

    command = [sys.executable, str(stub)]
    result: dict[str, tuple[str, int]] = {}

    def invoke() -> None:
        result["value"] = inner_loop_module._run_codex(
            command,
            "prompt",
            agent_log,
            environ=os.environ.copy(),
        )

    worker = threading.Thread(target=invoke)
    worker.start()

    saw_first_line_while_running = False
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if agent_log.exists():
            content = agent_log.read_text()
            if "first line" in content:
                saw_first_line_while_running = worker.is_alive()
                if saw_first_line_while_running:
                    break
        if not worker.is_alive():
            break
        time.sleep(0.02)

    worker.join(timeout=2.0)
    assert not worker.is_alive()
    assert saw_first_line_while_running

    output, exit_code = result["value"]
    assert exit_code == 0
    assert "first line" in output
    assert "second line" in output

    log_output = agent_log.read_text()
    assert "first line" in log_output
    assert "second line" in log_output


def test_fetch_pr_status_approves_from_allowlisted_comment(monkeypatch) -> None:
    payload = {
        "url": "https://github.com/acme/api/pull/42",
        "number": 42,
        "repository": {"owner": {"login": "acme"}, "name": "api"},
        "reviewDecision": "REVIEW_REQUIRED",
        "mergedAt": None,
        "latestReviews": [
            {"state": "CHANGES_REQUESTED", "submittedAt": "2026-02-09T00:00:00Z"}
        ],
        "comments": [
            {
                "author": {"login": "maintainer"},
                "body": "/approve",
                "createdAt": "2026-02-09T01:00:00Z",
            }
        ],
    }

    def fake_subprocess_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["gh", "pr", "view"],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr(inner_loop_module.subprocess, "run", fake_subprocess_run)
    settings = inner_loop_module.CommentApprovalSettings(
        allowed_usernames=("maintainer",),
        pattern_text=r"^\s*/approve\b",
        approval_regex=re.compile(r"^\s*/approve\b", re.IGNORECASE),
        review_actor_usernames=("*",),
    )
    updated, approved_by_comment, approved_by = (
        inner_loop_module._fetch_pr_status_with_gh_with_context(
            RunPR(url="https://github.com/acme/api/pull/42"),
            comment_approval=settings,
        )
    )

    assert updated.review_status == "approved"
    assert approved_by_comment is True
    assert approved_by == "maintainer"


def test_fetch_pr_status_reacts_with_thumbs_up_for_allowlisted_comment(
    monkeypatch,
) -> None:
    payload = {
        "url": "https://github.com/acme/api/pull/42",
        "number": 42,
        "reviewDecision": "REVIEW_REQUIRED",
        "mergedAt": None,
        "latestReviews": [],
        "comments": [
            {
                "id": "IC_kwDOAAABBBCCCDD",
                "author": {"login": "maintainer"},
                "body": "/approve",
                "createdAt": "2026-02-09T01:00:00Z",
                "reactionGroups": [],
            }
        ],
    }
    calls: list[list[str]] = []

    def fake_subprocess_run(args, **_kwargs):
        call_args = [str(part) for part in args]
        calls.append(call_args)
        if call_args[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(
                args=call_args,
                returncode=0,
                stdout=json.dumps(payload),
                stderr="",
            )
        if call_args[:3] == ["gh", "api", "graphql"]:
            return subprocess.CompletedProcess(
                args=call_args,
                returncode=0,
                stdout=json.dumps({"data": {"addReaction": {"reaction": {"content": "THUMBS_UP"}}}}),
                stderr="",
            )
        raise AssertionError(f"unexpected subprocess args: {call_args}")

    monkeypatch.setattr(inner_loop_module.subprocess, "run", fake_subprocess_run)
    settings = inner_loop_module.CommentApprovalSettings(
        allowed_usernames=("maintainer",),
        pattern_text=r"^\s*/approve\b",
        approval_regex=re.compile(r"^\s*/approve\b", re.IGNORECASE),
        review_actor_usernames=("*",),
    )
    updated, approved_by_comment, approved_by = (
        inner_loop_module._fetch_pr_status_with_gh_with_context(
            RunPR(url="https://github.com/acme/api/pull/42"),
            comment_approval=settings,
        )
    )

    assert updated.review_status == "approved"
    assert approved_by_comment is True
    assert approved_by == "maintainer"
    reaction_call = next(
        (call for call in calls if call[:3] == ["gh", "api", "graphql"]),
        None,
    )
    assert reaction_call is not None
    assert "subjectId=IC_kwDOAAABBBCCCDD" in reaction_call


def test_fetch_pr_status_skips_reaction_when_viewer_already_reacted(
    monkeypatch,
) -> None:
    payload = {
        "url": "https://github.com/acme/api/pull/42",
        "number": 42,
        "reviewDecision": "REVIEW_REQUIRED",
        "mergedAt": None,
        "latestReviews": [],
        "comments": [
            {
                "id": "IC_kwDOAAABBBCCCDD",
                "author": {"login": "maintainer"},
                "body": "/approve",
                "createdAt": "2026-02-09T01:00:00Z",
                "reactionGroups": [
                    {
                        "content": "THUMBS_UP",
                        "viewerHasReacted": True,
                    }
                ],
            }
        ],
    }
    calls: list[list[str]] = []

    def fake_subprocess_run(args, **_kwargs):
        call_args = [str(part) for part in args]
        calls.append(call_args)
        if call_args[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(
                args=call_args,
                returncode=0,
                stdout=json.dumps(payload),
                stderr="",
            )
        if call_args[:3] == ["gh", "api", "graphql"]:
            raise AssertionError("reaction call should be skipped when already reacted")
        raise AssertionError(f"unexpected subprocess args: {call_args}")

    monkeypatch.setattr(inner_loop_module.subprocess, "run", fake_subprocess_run)
    settings = inner_loop_module.CommentApprovalSettings(
        allowed_usernames=("maintainer",),
        pattern_text=r"^\s*/approve\b",
        approval_regex=re.compile(r"^\s*/approve\b", re.IGNORECASE),
        review_actor_usernames=("*",),
    )
    updated, approved_by_comment, approved_by = (
        inner_loop_module._fetch_pr_status_with_gh_with_context(
            RunPR(url="https://github.com/acme/api/pull/42"),
            comment_approval=settings,
        )
    )

    assert updated.review_status == "approved"
    assert approved_by_comment is True
    assert approved_by == "maintainer"
    assert all(call[:3] != ["gh", "api", "graphql"] for call in calls)


def test_fetch_pr_status_reaction_failure_does_not_block_approval(
    monkeypatch,
) -> None:
    payload = {
        "url": "https://github.com/acme/api/pull/42",
        "number": 42,
        "reviewDecision": "REVIEW_REQUIRED",
        "mergedAt": None,
        "latestReviews": [],
        "comments": [
            {
                "id": "IC_kwDOAAABBBCCCDD",
                "author": {"login": "maintainer"},
                "body": "/approve",
                "createdAt": "2026-02-09T01:00:00Z",
                "reactionGroups": [],
            }
        ],
    }

    def fake_subprocess_run(args, **_kwargs):
        call_args = [str(part) for part in args]
        if call_args[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(
                args=call_args,
                returncode=0,
                stdout=json.dumps(payload),
                stderr="",
            )
        if call_args[:3] == ["gh", "api", "graphql"]:
            return subprocess.CompletedProcess(
                args=call_args,
                returncode=1,
                stdout="",
                stderr="rate limit reached",
            )
        raise AssertionError(f"unexpected subprocess args: {call_args}")

    monkeypatch.setattr(inner_loop_module.subprocess, "run", fake_subprocess_run)
    settings = inner_loop_module.CommentApprovalSettings(
        allowed_usernames=("maintainer",),
        pattern_text=r"^\s*/approve\b",
        approval_regex=re.compile(r"^\s*/approve\b", re.IGNORECASE),
        review_actor_usernames=("*",),
    )
    messages: list[str] = []
    updated, approved_by_comment, approved_by = (
        inner_loop_module._fetch_pr_status_with_gh_with_context(
            RunPR(url="https://github.com/acme/api/pull/42"),
            comment_approval=settings,
            log_message=messages.append,
        )
    )

    assert updated.review_status == "approved"
    assert approved_by_comment is True
    assert approved_by == "maintainer"
    assert any(
        "failed to add thumbs-up reaction to approval comment" in message
        for message in messages
    )


def test_fetch_pr_status_skips_reaction_when_approval_comment_missing_node_id(
    monkeypatch,
) -> None:
    payload = {
        "url": "https://github.com/acme/api/pull/42",
        "number": 42,
        "reviewDecision": "REVIEW_REQUIRED",
        "mergedAt": None,
        "latestReviews": [],
        "comments": [
            {
                # no id on purpose: reaction should be skipped safely
                "author": {"login": "maintainer"},
                "body": "/approve",
                "createdAt": "2026-02-09T01:00:00Z",
                "reactionGroups": [],
            }
        ],
    }
    calls: list[list[str]] = []
    messages: list[str] = []

    def fake_subprocess_run(args, **_kwargs):
        call_args = [str(part) for part in args]
        calls.append(call_args)
        if call_args[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(
                args=call_args,
                returncode=0,
                stdout=json.dumps(payload),
                stderr="",
            )
        if call_args[:3] == ["gh", "api", "graphql"]:
            raise AssertionError("reaction call should be skipped without node id")
        raise AssertionError(f"unexpected subprocess args: {call_args}")

    monkeypatch.setattr(inner_loop_module.subprocess, "run", fake_subprocess_run)
    settings = inner_loop_module.CommentApprovalSettings(
        allowed_usernames=("maintainer",),
        pattern_text=r"^\s*/approve\b",
        approval_regex=re.compile(r"^\s*/approve\b", re.IGNORECASE),
        review_actor_usernames=("*",),
    )
    updated, approved_by_comment, approved_by = (
        inner_loop_module._fetch_pr_status_with_gh_with_context(
            RunPR(url="https://github.com/acme/api/pull/42"),
            comment_approval=settings,
            log_message=messages.append,
        )
    )

    assert updated.review_status == "approved"
    assert approved_by_comment is True
    assert approved_by == "maintainer"
    assert all(call[:3] != ["gh", "api", "graphql"] for call in calls)
    assert any("missing node id" in message for message in messages)


def test_fetch_pr_status_duplicate_reaction_error_is_non_fatal(
    monkeypatch,
) -> None:
    payload = {
        "url": "https://github.com/acme/api/pull/42",
        "number": 42,
        "reviewDecision": "REVIEW_REQUIRED",
        "mergedAt": None,
        "latestReviews": [],
        "comments": [
            {
                "id": "IC_kwDOAAABBBCCCDD",
                "author": {"login": "maintainer"},
                "body": "/approve",
                "createdAt": "2026-02-09T01:00:00Z",
                "reactionGroups": [],
            }
        ],
    }
    messages: list[str] = []

    def fake_subprocess_run(args, **_kwargs):
        call_args = [str(part) for part in args]
        if call_args[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(
                args=call_args,
                returncode=0,
                stdout=json.dumps(payload),
                stderr="",
            )
        if call_args[:3] == ["gh", "api", "graphql"]:
            return subprocess.CompletedProcess(
                args=call_args,
                returncode=1,
                stdout="",
                stderr="Reaction already exists for this user and content",
            )
        raise AssertionError(f"unexpected subprocess args: {call_args}")

    monkeypatch.setattr(inner_loop_module.subprocess, "run", fake_subprocess_run)
    settings = inner_loop_module.CommentApprovalSettings(
        allowed_usernames=("maintainer",),
        pattern_text=r"^\s*/approve\b",
        approval_regex=re.compile(r"^\s*/approve\b", re.IGNORECASE),
        review_actor_usernames=("*",),
    )
    updated, approved_by_comment, approved_by = (
        inner_loop_module._fetch_pr_status_with_gh_with_context(
            RunPR(url="https://github.com/acme/api/pull/42"),
            comment_approval=settings,
            log_message=messages.append,
        )
    )

    assert updated.review_status == "approved"
    assert approved_by_comment is True
    assert approved_by == "maintainer"
    assert any("already exists on approval comment" in message for message in messages)


def test_fetch_pr_status_approval_review_does_not_attempt_comment_reaction(
    monkeypatch,
) -> None:
    payload = {
        "url": "https://github.com/acme/api/pull/42",
        "number": 42,
        "reviewDecision": "REVIEW_REQUIRED",
        "mergedAt": None,
        "latestReviews": [],
        "reviews": [
            {
                "id": "PRR_kwDOAAABBBCCCDD",
                "author": {"login": "maintainer"},
                "state": "COMMENTED",
                "body": "/approve",
                "submittedAt": "2026-02-09T01:00:00Z",
            }
        ],
        "comments": [],
    }
    calls: list[list[str]] = []

    def fake_subprocess_run(args, **_kwargs):
        call_args = [str(part) for part in args]
        calls.append(call_args)
        if call_args[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(
                args=call_args,
                returncode=0,
                stdout=json.dumps(payload),
                stderr="",
            )
        if call_args[:3] == ["gh", "api", "graphql"]:
            raise AssertionError("comment reaction should not run for review-based approval")
        raise AssertionError(f"unexpected subprocess args: {call_args}")

    monkeypatch.setattr(inner_loop_module.subprocess, "run", fake_subprocess_run)
    settings = inner_loop_module.CommentApprovalSettings(
        allowed_usernames=("maintainer",),
        pattern_text=r"^\s*/approve\b",
        approval_regex=re.compile(r"^\s*/approve\b", re.IGNORECASE),
        review_actor_usernames=("*",),
    )
    updated, approved_by_comment, approved_by = (
        inner_loop_module._fetch_pr_status_with_gh_with_context(
            RunPR(url="https://github.com/acme/api/pull/42"),
            comment_approval=settings,
        )
    )

    assert updated.review_status == "approved"
    assert approved_by_comment is True
    assert approved_by == "maintainer"
    assert all(call[:3] != ["gh", "api", "graphql"] for call in calls)


def test_fetch_pr_status_approves_from_allowlisted_review(monkeypatch) -> None:
    payload = {
        "url": "https://github.com/acme/api/pull/42",
        "number": 42,
        "reviewDecision": "REVIEW_REQUIRED",
        "mergedAt": None,
        "latestReviews": [
            {"state": "CHANGES_REQUESTED", "submittedAt": "2026-02-09T00:00:00Z"}
        ],
        "reviews": [
            {
                "author": {"login": "maintainer"},
                "state": "COMMENTED",
                "body": "/approve",
                "submittedAt": "2026-02-09T01:00:00Z",
            }
        ],
        "comments": [],
    }

    def fake_subprocess_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["gh", "pr", "view"],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr(inner_loop_module.subprocess, "run", fake_subprocess_run)
    settings = inner_loop_module.CommentApprovalSettings(
        allowed_usernames=("maintainer",),
        pattern_text=r"^\s*/approve\b",
        approval_regex=re.compile(r"^\s*/approve\b", re.IGNORECASE),
        review_actor_usernames=("*",),
    )
    updated, approved_by_comment, approved_by = (
        inner_loop_module._fetch_pr_status_with_gh_with_context(
            RunPR(url="https://github.com/acme/api/pull/42"),
            comment_approval=settings,
        )
    )

    assert updated.review_status == "approved"
    assert updated.latest_review_submitted_at == "2026-02-09T01:00:00Z"
    assert approved_by_comment is True
    assert approved_by == "maintainer"


def test_fetch_pr_status_uses_newest_allowlisted_approval_signal(
    monkeypatch,
) -> None:
    payload = {
        "url": "https://github.com/acme/api/pull/42",
        "number": 42,
        "reviewDecision": "REVIEW_REQUIRED",
        "mergedAt": None,
        "latestReviews": [
            {"state": "CHANGES_REQUESTED", "submittedAt": "2026-02-09T00:00:00Z"}
        ],
        "reviews": [
            {
                "author": {"login": "maintainer"},
                "state": "COMMENTED",
                "body": "/approve",
                "submittedAt": "2026-02-09T02:00:00Z",
            }
        ],
        "comments": [
            {
                "author": {"login": "maintainer"},
                "body": "/approve",
                "createdAt": "2026-02-09T01:00:00Z",
            }
        ],
    }

    def fake_subprocess_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["gh", "pr", "view"],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr(inner_loop_module.subprocess, "run", fake_subprocess_run)
    settings = inner_loop_module.CommentApprovalSettings(
        allowed_usernames=("maintainer",),
        pattern_text=r"^\s*/approve\b",
        approval_regex=re.compile(r"^\s*/approve\b", re.IGNORECASE),
        review_actor_usernames=("*",),
    )
    updated, approved_by_comment, approved_by = (
        inner_loop_module._fetch_pr_status_with_gh_with_context(
            RunPR(url="https://github.com/acme/api/pull/42"),
            comment_approval=settings,
        )
    )

    assert updated.review_status == "approved"
    assert updated.latest_review_submitted_at == "2026-02-09T02:00:00Z"
    assert approved_by_comment is True
    assert approved_by == "maintainer"


def test_fetch_pr_status_ignores_allowlisted_review_older_than_changes_requested(
    monkeypatch,
) -> None:
    payload = {
        "url": "https://github.com/acme/api/pull/42",
        "number": 42,
        "reviewDecision": "REVIEW_REQUIRED",
        "mergedAt": None,
        "latestReviews": [
            {"state": "CHANGES_REQUESTED", "submittedAt": "2026-02-09T02:00:00Z"}
        ],
        "reviews": [
            {
                "author": {"login": "maintainer"},
                "state": "COMMENTED",
                "body": "/approve",
                "submittedAt": "2026-02-09T01:00:00Z",
            }
        ],
        "comments": [],
    }

    def fake_subprocess_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["gh", "pr", "view"],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr(inner_loop_module.subprocess, "run", fake_subprocess_run)
    settings = inner_loop_module.CommentApprovalSettings(
        allowed_usernames=("maintainer",),
        pattern_text=r"^\s*/approve\b",
        approval_regex=re.compile(r"^\s*/approve\b", re.IGNORECASE),
        review_actor_usernames=("*",),
    )
    updated, approved_by_comment, approved_by = (
        inner_loop_module._fetch_pr_status_with_gh_with_context(
            RunPR(url="https://github.com/acme/api/pull/42"),
            comment_approval=settings,
        )
    )

    assert updated.review_status == "open"
    assert approved_by_comment is False
    assert approved_by == ""


def test_fetch_pr_status_uses_plain_comment_as_feedback_signal(monkeypatch) -> None:
    payload = {
        "url": "https://github.com/acme/api/pull/42",
        "number": 42,
        "reviewDecision": "REVIEW_REQUIRED",
        "mergedAt": None,
        "latestReviews": [],
        "comments": [
            {
                "author": {"login": "maintainer"},
                "body": "please resolve conflicts",
                "createdAt": "2026-02-09T01:00:00Z",
            }
        ],
    }

    def fake_subprocess_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["gh", "pr", "view"],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr(inner_loop_module.subprocess, "run", fake_subprocess_run)
    settings = inner_loop_module.CommentApprovalSettings(
        allowed_usernames=("maintainer",),
        pattern_text=r"^\s*/approve\b",
        approval_regex=re.compile(r"^\s*/approve\b", re.IGNORECASE),
        review_actor_usernames=("*",),
    )
    messages: list[str] = []
    updated, approved_by_comment, approved_by = (
        inner_loop_module._fetch_pr_status_with_gh_with_context(
            RunPR(url="https://github.com/acme/api/pull/42"),
            comment_approval=settings,
            log_message=messages.append,
        )
    )

    assert updated.review_status == "open"
    assert updated.latest_review_submitted_at == "2026-02-09T01:00:00Z"
    assert approved_by_comment is False
    assert approved_by == ""
    assert any(
        "using latest plain PR comment as feedback signal" in message
        for message in messages
    )


def test_fetch_pr_status_uses_commented_review_as_feedback_signal(monkeypatch) -> None:
    payload = {
        "url": "https://github.com/acme/api/pull/42",
        "number": 42,
        "reviewDecision": "REVIEW_REQUIRED",
        "mergedAt": None,
        "latestReviews": [],
        "reviews": [
            {
                "author": {"login": "reviewer"},
                "state": "COMMENTED",
                "submittedAt": "2026-02-09T02:00:00Z",
            }
        ],
        "comments": [],
    }

    def fake_subprocess_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["gh", "pr", "view"],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr(inner_loop_module.subprocess, "run", fake_subprocess_run)
    settings = inner_loop_module.CommentApprovalSettings(
        allowed_usernames=(),
        pattern_text=r"^\s*/approve\b",
        approval_regex=re.compile(r"^\s*/approve\b", re.IGNORECASE),
        review_actor_usernames=("*",),
    )
    messages: list[str] = []
    updated, approved_by_comment, approved_by = (
        inner_loop_module._fetch_pr_status_with_gh_with_context(
            RunPR(url="https://github.com/acme/api/pull/42"),
            comment_approval=settings,
            log_message=messages.append,
        )
    )

    assert updated.review_status == "open"
    assert updated.latest_review_submitted_at == "2026-02-09T02:00:00Z"
    assert approved_by_comment is False
    assert approved_by == ""
    assert any(
        "using latest COMMENTED review as feedback signal" in message
        for message in messages
    )


def test_fetch_pr_status_prefers_newest_timestamp_across_feedback_sources(
    monkeypatch,
) -> None:
    payload = {
        "url": "https://github.com/acme/api/pull/42",
        "number": 42,
        "reviewDecision": "REVIEW_REQUIRED",
        "mergedAt": None,
        "latestReviews": [],
        "reviews": [
            {
                "author": {"login": "reviewer"},
                "state": "COMMENTED",
                "submittedAt": "2026-02-09T02:00:00Z",
            }
        ],
        "comments": [
            {
                "author": {"login": "maintainer"},
                "body": "please resolve conflicts",
                "createdAt": "2026-02-09T03:00:00Z",
            }
        ],
    }

    def fake_subprocess_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["gh", "pr", "view"],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr(inner_loop_module.subprocess, "run", fake_subprocess_run)
    settings = inner_loop_module.CommentApprovalSettings(
        allowed_usernames=(),
        pattern_text=r"^\s*/approve\b",
        approval_regex=re.compile(r"^\s*/approve\b", re.IGNORECASE),
        review_actor_usernames=("*",),
    )
    messages: list[str] = []
    updated, approved_by_comment, approved_by = (
        inner_loop_module._fetch_pr_status_with_gh_with_context(
            RunPR(url="https://github.com/acme/api/pull/42"),
            comment_approval=settings,
            log_message=messages.append,
        )
    )

    assert updated.review_status == "open"
    assert updated.latest_review_submitted_at == "2026-02-09T03:00:00Z"
    assert approved_by_comment is False
    assert approved_by == ""
    assert not any(
        "using latest COMMENTED review as feedback signal" in message
        for message in messages
    )
    assert any(
        "using latest plain PR comment as feedback signal" in message
        for message in messages
    )


def test_fetch_pr_status_logs_context_and_result(monkeypatch) -> None:
    payload = {
        "url": "https://github.com/acme/api/pull/42",
        "number": 42,
        "repository": {"owner": {"login": "acme"}, "name": "api"},
        "reviewDecision": "REVIEW_REQUIRED",
        "mergedAt": None,
        "latestReviews": [],
        "comments": [],
    }

    def fake_subprocess_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["gh", "pr", "view"],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr(inner_loop_module.subprocess, "run", fake_subprocess_run)
    settings = inner_loop_module.CommentApprovalSettings(
        allowed_usernames=("maintainer",),
        pattern_text=r"^\s*/approve\b",
        approval_regex=re.compile(r"^\s*/approve\b", re.IGNORECASE),
        review_actor_usernames=("*",),
    )
    messages: list[str] = []
    inner_loop_module._fetch_pr_status_with_gh_with_context(
        RunPR(url="https://github.com/acme/api/pull/42"),
        comment_approval=settings,
        log_message=messages.append,
    )

    assert any("polling PR status via gh" in message for message in messages)
    assert any("PR status poll result" in message for message in messages)


def test_fetch_pr_status_sets_ci_status_from_rollup(monkeypatch) -> None:
    payload = {
        "url": "https://github.com/acme/api/pull/42",
        "number": 42,
        "reviewDecision": "APPROVED",
        "mergedAt": None,
        "latestReviews": [],
        "comments": [],
        "statusCheckRollup": [
            {
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
            }
        ],
    }

    def fake_subprocess_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["gh", "pr", "view"],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr(inner_loop_module.subprocess, "run", fake_subprocess_run)
    settings = inner_loop_module.CommentApprovalSettings(
        allowed_usernames=(),
        pattern_text=r"^\s*/approve\b",
        approval_regex=re.compile(r"^\s*/approve\b", re.IGNORECASE),
        review_actor_usernames=("*",),
    )
    updated, approved_by_comment, approved_by = (
        inner_loop_module._fetch_pr_status_with_gh_with_context(
            RunPR(url="https://github.com/acme/api/pull/42"),
            comment_approval=settings,
        )
    )

    assert updated.review_status == "approved"
    assert updated.ci_status == "success"
    assert updated.ci_last_checked_at is not None
    assert approved_by_comment is False
    assert approved_by == ""


def test_fetch_pr_status_sets_ci_status_from_status_context_state(monkeypatch) -> None:
    payload = {
        "url": "https://github.com/acme/api/pull/42",
        "number": 42,
        "reviewDecision": "APPROVED",
        "mergedAt": None,
        "latestReviews": [],
        "comments": [],
        "statusCheckRollup": [
            {
                "__typename": "StatusContext",
                "context": "CodeRabbit",
                "state": "SUCCESS",
                "targetUrl": "",
            }
        ],
    }

    def fake_subprocess_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["gh", "pr", "view"],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr(inner_loop_module.subprocess, "run", fake_subprocess_run)
    settings = inner_loop_module.CommentApprovalSettings(
        allowed_usernames=(),
        pattern_text=r"^\s*/approve\b",
        approval_regex=re.compile(r"^\s*/approve\b", re.IGNORECASE),
        review_actor_usernames=("*",),
    )
    updated, approved_by_comment, approved_by = (
        inner_loop_module._fetch_pr_status_with_gh_with_context(
            RunPR(url="https://github.com/acme/api/pull/42"),
            comment_approval=settings,
        )
    )

    assert updated.review_status == "approved"
    assert updated.ci_status == "success"
    assert updated.ci_last_checked_at is not None
    assert approved_by_comment is False
    assert approved_by == ""


def test_fetch_pr_status_uses_supported_gh_json_fields(monkeypatch) -> None:
    captured_args: list[str] = []
    payload = {
        "url": "https://github.com/acme/api/pull/42",
        "number": 42,
        "reviewDecision": "REVIEW_REQUIRED",
        "mergedAt": None,
        "latestReviews": [],
        "comments": [],
    }

    def fake_subprocess_run(args, **_kwargs):
        nonlocal captured_args
        captured_args = list(args)
        return subprocess.CompletedProcess(
            args=["gh", "pr", "view"],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr(inner_loop_module.subprocess, "run", fake_subprocess_run)
    settings = inner_loop_module.CommentApprovalSettings(
        allowed_usernames=(),
        pattern_text=r"^\s*/approve\b",
        approval_regex=re.compile(r"^\s*/approve\b", re.IGNORECASE),
        review_actor_usernames=("*",),
    )
    updated, _approved_by_comment, _approved_by = (
        inner_loop_module._fetch_pr_status_with_gh_with_context(
            RunPR(url="https://github.com/acme/api/pull/42"),
            comment_approval=settings,
        )
    )

    assert updated.repo == "acme/api"
    assert "--json" in captured_args
    json_fields = captured_args[captured_args.index("--json") + 1]
    assert "repository" not in json_fields


def test_fetch_pr_status_ignores_non_allowlisted_comment(monkeypatch) -> None:
    payload = {
        "url": "https://github.com/acme/api/pull/42",
        "number": 42,
        "repository": {"owner": {"login": "acme"}, "name": "api"},
        "reviewDecision": "REVIEW_REQUIRED",
        "mergedAt": None,
        "latestReviews": [],
        "comments": [
            {
                "author": {"login": "random-user"},
                "body": "/approve",
                "createdAt": "2026-02-09T01:00:00Z",
            }
        ],
    }

    def fake_subprocess_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["gh", "pr", "view"],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr(inner_loop_module.subprocess, "run", fake_subprocess_run)
    settings = inner_loop_module.CommentApprovalSettings(
        allowed_usernames=("maintainer",),
        pattern_text=r"^\s*/approve\b",
        approval_regex=re.compile(r"^\s*/approve\b", re.IGNORECASE),
        review_actor_usernames=("*",),
    )
    updated, approved_by_comment, _approved_by = (
        inner_loop_module._fetch_pr_status_with_gh_with_context(
            RunPR(url="https://github.com/acme/api/pull/42"),
            comment_approval=settings,
        )
    )

    assert updated.review_status == "open"
    assert approved_by_comment is False


def test_fetch_pr_status_requires_exact_allowlisted_username(monkeypatch) -> None:
    payload = {
        "url": "https://github.com/acme/api/pull/42",
        "number": 42,
        "repository": {"owner": {"login": "acme"}, "name": "api"},
        "reviewDecision": "REVIEW_REQUIRED",
        "mergedAt": None,
        "latestReviews": [],
        "comments": [
            {
                "author": {"login": "acme1"},
                "body": "/approve",
                "createdAt": "2026-02-09T01:00:00Z",
            }
        ],
    }

    def fake_subprocess_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["gh", "pr", "view"],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr(inner_loop_module.subprocess, "run", fake_subprocess_run)
    settings = inner_loop_module.CommentApprovalSettings(
        allowed_usernames=("acme",),
        pattern_text=r"^\s*/approve\b",
        approval_regex=re.compile(r"^\s*/approve\b", re.IGNORECASE),
        review_actor_usernames=("*",),
    )
    updated, approved_by_comment, _approved_by = (
        inner_loop_module._fetch_pr_status_with_gh_with_context(
            RunPR(url="https://github.com/acme/api/pull/42"),
            comment_approval=settings,
        )
    )

    assert updated.review_status == "open"
    assert approved_by_comment is False


def test_fetch_pr_status_filters_review_events_by_provider_allowlist(
    monkeypatch,
) -> None:
    payload = {
        "url": "https://github.com/acme/api/pull/42",
        "number": 42,
        "reviewDecision": "CHANGES_REQUESTED",
        "mergedAt": None,
        "latestReviews": [
            {
                "author": {"login": "random-user"},
                "state": "CHANGES_REQUESTED",
                "submittedAt": "2026-02-09T03:00:00Z",
            }
        ],
        "reviews": [
            {
                "author": {"login": "random-user"},
                "state": "CHANGES_REQUESTED",
                "submittedAt": "2026-02-09T03:00:00Z",
            }
        ],
        "comments": [
            {
                "author": {"login": "random-user"},
                "body": "untrusted feedback",
                "createdAt": "2026-02-09T04:00:00Z",
            },
            {
                "author": {"login": "maintainer"},
                "body": "trusted feedback",
                "createdAt": "2026-02-09T02:00:00Z",
            },
        ],
    }

    def fake_subprocess_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["gh", "pr", "view"],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr(inner_loop_module.subprocess, "run", fake_subprocess_run)
    settings = inner_loop_module.CommentApprovalSettings(
        allowed_usernames=(),
        pattern_text=r"^\s*/approve\b",
        approval_regex=re.compile(r"^\s*/approve\b", re.IGNORECASE),
        review_actor_usernames=("maintainer",),
    )
    updated, approved_by_comment, _approved_by = (
        inner_loop_module._fetch_pr_status_with_gh_with_context(
            RunPR(url="https://github.com/acme/api/pull/42"),
            comment_approval=settings,
        )
    )

    assert updated.review_status == "open"
    assert updated.latest_review_submitted_at == "2026-02-09T02:00:00Z"
    assert approved_by_comment is False


def test_fetch_pr_status_ignores_unallowlisted_changes_requested_when_filtered(
    monkeypatch,
) -> None:
    payload = {
        "url": "https://github.com/acme/api/pull/42",
        "number": 42,
        "reviewDecision": "CHANGES_REQUESTED",
        "mergedAt": None,
        "latestReviews": [
            {
                "author": {"login": "random-user"},
                "state": "CHANGES_REQUESTED",
                "submittedAt": "2026-02-09T03:00:00Z",
            }
        ],
        "reviews": [
            {
                "author": {"login": "random-user"},
                "state": "CHANGES_REQUESTED",
                "submittedAt": "2026-02-09T03:00:00Z",
            }
        ],
        "comments": [],
    }

    def fake_subprocess_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["gh", "pr", "view"],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr(inner_loop_module.subprocess, "run", fake_subprocess_run)
    settings = inner_loop_module.CommentApprovalSettings(
        allowed_usernames=(),
        pattern_text=r"^\s*/approve\b",
        approval_regex=re.compile(r"^\s*/approve\b", re.IGNORECASE),
        review_actor_usernames=("maintainer",),
    )
    updated, approved_by_comment, _approved_by = (
        inner_loop_module._fetch_pr_status_with_gh_with_context(
            RunPR(url="https://github.com/acme/api/pull/42"),
            comment_approval=settings,
        )
    )

    assert updated.review_status == "open"
    assert updated.latest_review_submitted_at is None
    assert approved_by_comment is False


def test_fetch_pr_status_denies_all_review_events_when_allowlist_empty(
    monkeypatch,
) -> None:
    payload = {
        "url": "https://github.com/acme/api/pull/42",
        "number": 42,
        "reviewDecision": "CHANGES_REQUESTED",
        "mergedAt": None,
        "latestReviews": [
            {
                "author": {"login": "reviewer"},
                "state": "CHANGES_REQUESTED",
                "submittedAt": "2026-02-09T03:00:00Z",
            }
        ],
        "reviews": [
            {
                "author": {"login": "maintainer"},
                "state": "COMMENTED",
                "body": "/approve",
                "submittedAt": "2026-02-09T04:00:00Z",
            }
        ],
        "comments": [
            {
                "author": {"login": "maintainer"},
                "body": "/approve",
                "createdAt": "2026-02-09T05:00:00Z",
            }
        ],
    }

    def fake_subprocess_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["gh", "pr", "view"],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr(inner_loop_module.subprocess, "run", fake_subprocess_run)
    settings = inner_loop_module.CommentApprovalSettings(
        allowed_usernames=("maintainer",),
        pattern_text=r"^\s*/approve\b",
        approval_regex=re.compile(r"^\s*/approve\b", re.IGNORECASE),
        review_actor_usernames=(),
    )
    updated, approved_by_comment, _approved_by = (
        inner_loop_module._fetch_pr_status_with_gh_with_context(
            RunPR(url="https://github.com/acme/api/pull/42"),
            comment_approval=settings,
        )
    )

    assert updated.review_status == "open"
    assert updated.latest_review_submitted_at is None
    assert approved_by_comment is False


def test_fetch_pr_status_uses_latest_review_per_author_when_latest_reviews_missing(
    monkeypatch,
) -> None:
    payload = {
        "url": "https://github.com/acme/api/pull/42",
        "number": 42,
        "reviewDecision": "CHANGES_REQUESTED",
        "mergedAt": None,
        "latestReviews": [],
        "reviews": [
            {
                "author": {"login": "maintainer"},
                "state": "CHANGES_REQUESTED",
                "submittedAt": "2026-02-09T01:00:00Z",
            },
            {
                "author": {"login": "maintainer"},
                "state": "APPROVED",
                "submittedAt": "2026-02-09T02:00:00Z",
            },
        ],
        "comments": [],
    }

    def fake_subprocess_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["gh", "pr", "view"],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr(inner_loop_module.subprocess, "run", fake_subprocess_run)
    settings = inner_loop_module.CommentApprovalSettings(
        allowed_usernames=(),
        pattern_text=r"^\s*/approve\b",
        approval_regex=re.compile(r"^\s*/approve\b", re.IGNORECASE),
        review_actor_usernames=("maintainer",),
    )
    updated, approved_by_comment, _approved_by = (
        inner_loop_module._fetch_pr_status_with_gh_with_context(
            RunPR(url="https://github.com/acme/api/pull/42"),
            comment_approval=settings,
        )
    )

    assert updated.review_status == "approved"
    assert updated.latest_review_submitted_at == "2026-02-09T02:00:00Z"
    assert approved_by_comment is False


def test_fetch_pr_status_does_not_override_newer_changes_requested(monkeypatch) -> None:
    payload = {
        "url": "https://github.com/acme/api/pull/42",
        "number": 42,
        "repository": {"owner": {"login": "acme"}, "name": "api"},
        "reviewDecision": "CHANGES_REQUESTED",
        "mergedAt": None,
        "latestReviews": [
            {"state": "CHANGES_REQUESTED", "submittedAt": "2026-02-09T02:00:00Z"}
        ],
        "comments": [
            {
                "author": {"login": "maintainer"},
                "body": "/approve",
                "createdAt": "2026-02-09T01:00:00Z",
            }
        ],
    }

    def fake_subprocess_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["gh", "pr", "view"],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr(inner_loop_module.subprocess, "run", fake_subprocess_run)
    settings = inner_loop_module.CommentApprovalSettings(
        allowed_usernames=("maintainer",),
        pattern_text=r"^\s*/approve\b",
        approval_regex=re.compile(r"^\s*/approve\b", re.IGNORECASE),
        review_actor_usernames=("*",),
    )
    updated, approved_by_comment, _approved_by = (
        inner_loop_module._fetch_pr_status_with_gh_with_context(
            RunPR(url="https://github.com/acme/api/pull/42"),
            comment_approval=settings,
        )
    )

    assert updated.review_status == "changes_requested"
    assert approved_by_comment is False


def test_load_comment_approval_settings_invalid_pattern_falls_back(tmp_path) -> None:
    runtime_config = InnerLoopRuntimeConfig(
        approval_comment_usernames=("maintainer",),
        approval_comment_pattern="[",
        review_actor_usernames=("reviewer",),
    )
    settings = inner_loop_module._load_comment_approval_settings(
        runtime_config=runtime_config
    )

    assert settings.allowed_usernames == ("maintainer",)
    assert settings.review_actor_usernames == ("reviewer",)
    assert settings.used_default_pattern is True
    assert settings.pattern_text == DEFAULT_APPROVAL_COMMENT_PATTERN


def test_inner_loop_module_main_delegates_to_click_command(tmp_path, monkeypatch) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    captured: dict[str, object] = {}

    def fake_inner_loop_command(
        run_dir: Path | None,
        prompt_file: Path | None,
        reset: bool,
    ) -> None:
        captured["run_dir"] = run_dir
        captured["prompt_file"] = prompt_file
        captured["reset"] = reset

    inner_loop_command = cli_module.main.commands["inner-loop"]
    monkeypatch.setattr(inner_loop_command, "callback", fake_inner_loop_command)
    monkeypatch.setattr(inner_loop_module.sys, "argv", [
        "loops.core.inner_loop",
        "--run-dir",
        str(run_dir),
        "--reset",
    ])

    with pytest.raises(SystemExit) as exc_info:
        inner_loop_module.main()
    assert exc_info.value.code == 0

    assert captured["run_dir"] == run_dir
    assert captured["prompt_file"] is None
    assert captured["reset"] is True
