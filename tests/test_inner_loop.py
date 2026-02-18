from __future__ import annotations

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

from loops.approval_config import (
    DEFAULT_APPROVAL_COMMENT_PATTERN,
    INNER_LOOP_APPROVAL_CONFIG_FILE,
)
from loops import inner_loop as inner_loop_module
from loops.inner_loop import run_inner_loop
from loops.run_record import RunPR, RunRecord, Task, read_run_record, write_run_record
from loops.state_signal import enqueue_state_signal


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
    needs_user_input: bool = False,
    needs_user_input_payload: dict[str, object] | None = None,
) -> None:
    record = RunRecord(
        task=_task(),
        pr=pr,
        codex_session=None,
        needs_user_input=needs_user_input,
        needs_user_input_payload=needs_user_input_payload,
        last_state="NEEDS_INPUT" if needs_user_input else "RUNNING",
        updated_at="",
    )
    write_run_record(run_dir / "run.json", record)


def _write_codex_stub(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
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
                "counter_path = Path(os.environ['STUB_COUNTER_PATH'])",
                "if counter_path.exists():",
                "    count = int(counter_path.read_text())",
                "else:",
                "    count = 0",
                "count += 1",
                "counter_path.write_text(str(count))",
                "",
                "print(json.dumps({'session_id': f'session-{count}'}))",
                "if count == 1:",
                "    print('Opened PR https://github.com/acme/api/pull/42')",
                "else:",
                "    print('cleanup complete')",
                "sys.exit(0)",
                "",
            ]
        )
    )


def test_inner_loop_reaches_done_lifecycle(tmp_path, monkeypatch) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(run_dir)

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
                review_status="approved",
                merged_at=None,
                last_checked_at="2026-02-09T00:00:01Z",
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
    assert result.pr is not None
    assert result.pr.merged_at == "2026-02-09T00:00:02Z"
    assert counter_path.read_text() == "2"  # RUNNING + PR_APPROVED cleanup
    run_log = (run_dir / "run.log").read_text()
    assert re.search(
        r"^\d{4}-\d{2}-\d{2}T[0-9:.+-]+ \[loops\] iteration 1 enter: state=RUNNING",
        run_log,
        re.MULTILINE,
    )
    assert "iteration 1 enter: state=RUNNING" in run_log
    assert "exit: next_state=DONE action=done_exit" in run_log


def test_inner_loop_consumes_signal_and_uses_user_response_in_prompt(
    tmp_path, monkeypatch
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(run_dir)

    enqueue_state_signal(
        run_dir,
        state="NEEDS_INPUT",
        message="Need user decision",
        context={"scope": "priority"},
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
    assert (
        'If needing input from user, use "$needs_input" skill to request user input.'
        in prompts
    )
    assert "User input:\\nack: Need user decision" in prompts


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
    )

    enqueue_state_signal(
        run_dir,
        state="NEEDS_INPUT",
        message="Need user decision",
        context={"scope": "review"},
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


def test_apply_pending_signals_does_not_advance_offset_on_write_failure(
    tmp_path, monkeypatch
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(run_dir)

    enqueue_state_signal(
        run_dir,
        state="NEEDS_INPUT",
        message="Need user decision",
        context={"priority": "high"},
    )

    original_write_run_record = inner_loop_module.write_run_record
    attempts = {"count": 0}

    def flaky_write_run_record(path: Path, record: RunRecord) -> RunRecord:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("simulated write failure")
        return original_write_run_record(path, record)

    monkeypatch.setattr(inner_loop_module, "write_run_record", flaky_write_run_record)
    with pytest.raises(RuntimeError, match="simulated write failure"):
        inner_loop_module._apply_pending_signals(
            run_dir,
            read_run_record(run_dir / "run.json"),
        )

    offset_path = run_dir / inner_loop_module.SIGNAL_OFFSET_FILE
    assert not offset_path.exists()

    monkeypatch.setattr(inner_loop_module, "write_run_record", original_write_run_record)
    updated = inner_loop_module._apply_pending_signals(
        run_dir,
        read_run_record(run_dir / "run.json"),
    )
    assert updated.needs_user_input is True
    assert offset_path.exists()
    assert int(offset_path.read_text().strip()) > 0


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
        result["value"] = inner_loop_module._run_codex(command, "prompt", agent_log)

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
    )
    messages: list[str] = []
    inner_loop_module._fetch_pr_status_with_gh_with_context(
        RunPR(url="https://github.com/acme/api/pull/42"),
        comment_approval=settings,
        log_message=messages.append,
    )

    assert any("polling PR status via gh" in message for message in messages)
    assert any("PR status poll result" in message for message in messages)


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
    )
    updated, approved_by_comment, _approved_by = (
        inner_loop_module._fetch_pr_status_with_gh_with_context(
            RunPR(url="https://github.com/acme/api/pull/42"),
            comment_approval=settings,
        )
    )

    assert updated.review_status == "open"
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
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / INNER_LOOP_APPROVAL_CONFIG_FILE).write_text(
        json.dumps(
            {
                "approval_comment_usernames": ["Maintainer"],
                "approval_comment_pattern": "[",
            }
        )
    )

    settings = inner_loop_module._load_comment_approval_settings(run_dir)

    assert settings.allowed_usernames == ("maintainer",)
    assert settings.used_default_pattern is True
    assert settings.pattern_text == DEFAULT_APPROVAL_COMMENT_PATTERN
