from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

import pytest

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
