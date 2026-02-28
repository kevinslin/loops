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
from loops.handoff_handlers import HandoffResult
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
    codex_session: inner_loop_module.CodexSession | None = None,
    needs_user_input: bool = False,
    needs_user_input_payload: dict[str, object] | None = None,
) -> None:
    record = RunRecord(
        task=_task(),
        pr=pr,
        codex_session=codex_session,
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
    assert "Do not merge until the state is exactly <state>PR_APPROVED</state>." in prompts
    assert "User input:\\nack: Need user decision" in prompts
    assert "<state>RUNNING</state>" in prompts
    run_log = (run_dir / "run.log").read_text()
    assert '[loops] user input for codex turn: "ack: Need user decision"' in run_log


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
    assert "<state>WAITING_ON_REVIEW</state>" in prompts
    run_log = (run_dir / "run.log").read_text()
    assert '[loops] user input for codex turn: "ack: Need user decision"' in run_log


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

    def fake_run_codex(command: list[str], _prompt: str, _agent_log: Path) -> tuple[str, int]:
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
    )

    assert resume_fallback_used is True
    assert exit_code == 0
    assert calls == [
        ["codex", "exec", "resume", "stale-session"],
        ["codex", "exec"],
    ]
    assert inner_loop_module._extract_session_id(output) == "session-fresh"


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
    ) -> tuple[str, int, bool]:
        del base_command, prompt, agent_log, run_log, codex_session, turn_label
        return "resume failed\nfallback failed\n", 17, True

    monkeypatch.setattr(inner_loop_module, "_invoke_codex", fake_invoke_codex)
    run_json_path = run_dir / "run.json"
    updated = inner_loop_module._run_codex_turn(
        run_json_path=run_json_path,
        run_log=run_dir / "run.log",
        agent_log=run_dir / "agent.log",
        run_record=read_run_record(run_json_path),
        command=["codex", "exec"],
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
