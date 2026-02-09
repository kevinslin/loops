import shlex
import sys
from pathlib import Path

from loops.inner_loop import run_inner_loop
from loops.run_record import RunRecord, Task, read_run_record, write_run_record


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


def _write_stub(path: Path, *, session_id: str, exit_code: int) -> None:
    path.write_text(
        "\n".join(
            [
                "import sys",
                "sys.stdin.read()",
                f"print('session_id: {session_id}')",
                "print('stub output')",
                f"sys.exit({exit_code})",
                "",
            ]
        )
    )


def _write_run_record(run_dir: Path, *, needs_user_input: bool = False) -> None:
    record = RunRecord(
        task=_task(),
        pr=None,
        codex_session=None,
        needs_user_input=needs_user_input,
        last_state="NEEDS_INPUT" if needs_user_input else "RUNNING",
        updated_at="",
    )
    write_run_record(run_dir / "run.json", record)


def test_inner_loop_records_session_id_and_logs(tmp_path, monkeypatch) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(run_dir)

    session_id = "123e4567-e89b-12d3-a456-426614174000"
    stub = tmp_path / "codex_stub.py"
    _write_stub(stub, session_id=session_id, exit_code=0)

    monkeypatch.setenv(
        "CODEX_CMD",
        f"{shlex.quote(sys.executable)} {shlex.quote(str(stub))}",
    )

    run_inner_loop(run_dir)

    record = read_run_record(run_dir / "run.json")
    assert record.codex_session is not None
    assert record.codex_session.id == session_id

    log_output = (run_dir / "run.log").read_text()
    assert "stub output" in log_output


def test_inner_loop_sets_needs_user_input_on_failure(tmp_path, monkeypatch) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(run_dir)

    session_id = "123e4567-e89b-12d3-a456-426614174001"
    stub = tmp_path / "codex_stub.py"
    _write_stub(stub, session_id=session_id, exit_code=2)

    monkeypatch.setenv(
        "CODEX_CMD",
        f"{shlex.quote(sys.executable)} {shlex.quote(str(stub))}",
    )

    run_inner_loop(run_dir)

    record = read_run_record(run_dir / "run.json")
    assert record.needs_user_input is True


def test_inner_loop_clears_needs_user_input_on_success(tmp_path, monkeypatch) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_run_record(run_dir, needs_user_input=True)

    session_id = "123e4567-e89b-12d3-a456-426614174002"
    stub = tmp_path / "codex_stub.py"
    _write_stub(stub, session_id=session_id, exit_code=0)

    monkeypatch.setenv(
        "CODEX_CMD",
        f"{shlex.quote(sys.executable)} {shlex.quote(str(stub))}",
    )

    run_inner_loop(run_dir)

    record = read_run_record(run_dir / "run.json")
    assert record.needs_user_input is False
