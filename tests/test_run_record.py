import json

from loops.run_record import (
    CodexSession,
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
    pr = RunPR(url="https://example.com/pr/1", review_status="approved")
    assert derive_run_state(pr, False) == "DONE"


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
            "needs_user_input",
            "last_state",
            "updated_at",
        ]
    ).issubset(payload.keys())
    assert payload["pr"] is None
    assert payload["codex_session"]["id"] == "session-1"
    assert payload["last_state"] == updated.last_state
    assert payload["updated_at"]

    roundtrip = read_run_record(path)
    assert roundtrip.task.id == record.task.id
