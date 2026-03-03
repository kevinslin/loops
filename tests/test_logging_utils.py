from __future__ import annotations

import re
from pathlib import Path

import pytest

from loops.utils.logging import STREAM_LOGS_STDOUT_ENV, append_log


def test_append_log_writes_timestamped_lines(tmp_path: Path) -> None:
    log_path = tmp_path / "run.log"

    append_log(log_path, "line one\nline two")

    lines = log_path.read_text().splitlines()
    assert len(lines) == 2
    assert re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{2} ",
        lines[0],
    )
    assert "+" not in lines[0]
    assert "Z" not in lines[0]
    assert "line one" in lines[0]
    assert "line two" in lines[1]


def test_append_log_streams_to_stdout_when_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(STREAM_LOGS_STDOUT_ENV, "1")
    log_path = tmp_path / "run.log"

    append_log(log_path, "stream me")

    captured = capsys.readouterr()
    assert "stream me" in captured.out
    assert "stream me" in log_path.read_text()


def test_append_log_does_not_stream_without_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(STREAM_LOGS_STDOUT_ENV, raising=False)
    log_path = tmp_path / "run.log"

    append_log(log_path, "file only")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "file only" in log_path.read_text()
