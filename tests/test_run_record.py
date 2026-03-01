import json

import pytest

from loops.run_record import (
    CodexSession,
    RunAutoApprove,
    RunPR,
    RunRecord,
    Task,
    derive_run_state,
    read_run_record,
    write_run_record,
)


def _task() -> Task:
    return Task(
        provider_id="github",
        id="1",
        title="Test",
        status="ready",
        url="https://example.com/task/1",
        created_at="2026-02-03T00:00:00Z",
        updated_at="2026-02-03T00:00:00Z",
    )


def test_derive_run_state_needs_input() -> None:
    assert derive_run_state(None, True) == "NEEDS_INPUT"


def test_derive_run_state_done() -> None:
    pr = RunPR(
        url="https://example.com/pr/1",
        review_status="approved",
        merged_at="2026-02-03T00:00:00Z",
    )
    assert derive_run_state(pr, False) == "DONE"


def test_derive_run_state_pr_approved() -> None:
    pr = RunPR(
        url="https://example.com/pr/1",
        review_status="approved",
        ci_status="success",
    )
    assert derive_run_state(pr, False) == "PR_APPROVED"


def test_derive_run_state_waiting_on_review() -> None:
    for status in ["open", "changes_requested"]:
        pr = RunPR(url="https://example.com/pr/1", review_status=status)
        assert derive_run_state(pr, False) == "WAITING_ON_REVIEW"


def test_derive_run_state_running() -> None:
    assert derive_run_state(None, False) == "RUNNING"


def test_write_run_record_writes_required_keys(tmp_path) -> None:
    record = RunRecord(
        task=_task(),
        pr=None,
        codex_session=CodexSession(id="session-1"),
        needs_user_input=False,
        last_state="RUNNING",
        updated_at="",
    )
    path = tmp_path / "run.json"
    updated = write_run_record(path, record)

    payload = json.loads(path.read_text())
    assert set(
        [
            "task",
            "pr",
            "codex_session",
            "auto_approve",
            "needs_user_input",
            "needs_user_input_payload",
            "stream_logs_stdout",
            "last_state",
            "updated_at",
        ]
    ).issubset(payload.keys())
    assert payload["pr"] is None
    assert payload["codex_session"]["id"] == "session-1"
    assert payload["auto_approve"] is None
    assert payload["last_state"] == updated.last_state
    assert payload["updated_at"]

    roundtrip = read_run_record(path)
    assert roundtrip.task.id == record.task.id


def test_read_run_record_rejects_non_bool_needs_user_input(tmp_path) -> None:
    payload = {
        "task": _task().to_dict(),
        "pr": None,
        "codex_session": None,
        "needs_user_input": "false",
        "last_state": "RUNNING",
        "updated_at": "2026-02-03T00:00:00Z",
    }
    path = tmp_path / "run.json"
    path.write_text(json.dumps(payload))

    with pytest.raises(TypeError):
        read_run_record(path)


def test_read_run_record_rejects_non_bool_stream_logs_stdout(tmp_path) -> None:
    payload = {
        "task": _task().to_dict(),
        "pr": None,
        "codex_session": None,
        "needs_user_input": False,
        "stream_logs_stdout": "true",
        "last_state": "RUNNING",
        "updated_at": "2026-02-03T00:00:00Z",
    }
    path = tmp_path / "run.json"
    path.write_text(json.dumps(payload))

    with pytest.raises(TypeError):
        read_run_record(path)


def test_read_run_record_accepts_needs_user_input_payload(tmp_path) -> None:
    payload = {
        "task": _task().to_dict(),
        "pr": None,
        "codex_session": None,
        "needs_user_input": True,
        "needs_user_input_payload": {
            "message": "Need decision",
            "context": {"foo": "bar"},
        },
        "last_state": "NEEDS_INPUT",
        "updated_at": "2026-02-03T00:00:00Z",
    }
    path = tmp_path / "run.json"
    path.write_text(json.dumps(payload))

    record = read_run_record(path)
    assert record.needs_user_input_payload is not None
    assert record.needs_user_input_payload["message"] == "Need decision"


def test_run_record_round_trips_auto_approve_payload(tmp_path) -> None:
    record = RunRecord(
        task=_task(),
        pr=RunPR(
            url="https://example.com/pr/1",
            review_status="approved",
            ci_status="success",
        ),
        codex_session=CodexSession(id="session-1"),
        auto_approve=RunAutoApprove(
            verdict="APPROVE",
            impact=3,
            risk=2,
            size=2,
            judged_at="2026-02-09T00:00:01Z",
            summary="Auto-approve passed.",
        ),
        needs_user_input=False,
        last_state="RUNNING",
        updated_at="",
    )
    path = tmp_path / "run.json"
    write_run_record(path, record, auto_approve_enabled=True)

    restored = read_run_record(path)
    assert restored.auto_approve is not None
    assert restored.auto_approve.verdict == "APPROVE"
    assert restored.auto_approve.impact == 3
    assert restored.last_state == "PR_APPROVED"


def test_run_record_round_trips_stream_logs_stdout(tmp_path) -> None:
    record = RunRecord(
        task=_task(),
        pr=None,
        codex_session=CodexSession(id="session-1"),
        needs_user_input=False,
        stream_logs_stdout=True,
        last_state="RUNNING",
        updated_at="",
    )
    path = tmp_path / "run.json"
    write_run_record(path, record)

    payload = json.loads(path.read_text())
    assert payload["stream_logs_stdout"] is True

    restored = read_run_record(path)
    assert restored.stream_logs_stdout is True


def test_derive_run_state_keeps_manual_approval_path_when_auto_approve_enabled() -> None:
    pr = RunPR(
        url="https://example.com/pr/1",
        review_status="approved",
        ci_status="success",
    )
    assert derive_run_state(
        pr,
        False,
        auto_approve_enabled=True,
        auto_approve=RunAutoApprove(verdict="none"),
    ) == "PR_APPROVED"


def test_derive_run_state_allows_auto_approve_path_when_ci_green() -> None:
    pr = RunPR(
        url="https://example.com/pr/1",
        review_status="open",
        ci_status="success",
    )
    assert (
        derive_run_state(
            pr,
            False,
            auto_approve_enabled=True,
            auto_approve=RunAutoApprove(verdict="APPROVE"),
        )
        == "PR_APPROVED"
    )


def test_run_pr_round_trips_review_timestamp_fields() -> None:
    pr = RunPR(
        url="https://example.com/pr/1",
        review_status="changes_requested",
        latest_review_submitted_at="2026-02-09T01:00:00Z",
        review_addressed_at="2026-02-09T01:00:00Z",
    )
    d = pr.to_dict()
    assert d["latest_review_submitted_at"] == "2026-02-09T01:00:00Z"
    assert d["review_addressed_at"] == "2026-02-09T01:00:00Z"
    restored = RunPR.from_dict(d)
    assert restored.latest_review_submitted_at == "2026-02-09T01:00:00Z"
    assert restored.review_addressed_at == "2026-02-09T01:00:00Z"


def test_run_pr_from_dict_without_review_timestamp_fields() -> None:
    d = {"url": "https://example.com/pr/1", "review_status": "open"}
    pr = RunPR.from_dict(d)
    assert pr.latest_review_submitted_at is None
    assert pr.review_addressed_at is None


def test_read_run_record_rejects_invalid_needs_user_input_payload(tmp_path) -> None:
    payload = {
        "task": _task().to_dict(),
        "pr": None,
        "codex_session": None,
        "needs_user_input": True,
        "needs_user_input_payload": {"context": {"foo": "bar"}},
        "last_state": "NEEDS_INPUT",
        "updated_at": "2026-02-03T00:00:00Z",
    }
    path = tmp_path / "run.json"
    path.write_text(json.dumps(payload))

    with pytest.raises(TypeError):
        read_run_record(path)
