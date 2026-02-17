from __future__ import annotations

import json
import os
import re
import subprocess
import sys

import pytest

from loops.state_signal import SIGNAL_QUEUE_FILE, enqueue_state_signal


def test_enqueue_state_signal_writes_queue_and_log(tmp_path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    signal = enqueue_state_signal(
        run_dir,
        state="NEEDS_INPUT",
        message="Need approval",
        context={"foo": "bar"},
    )
    assert signal["state"] == "NEEDS_INPUT"

    queue_lines = (run_dir / SIGNAL_QUEUE_FILE).read_text().splitlines()
    assert len(queue_lines) == 1
    queued = json.loads(queue_lines[0])
    assert queued["payload"]["message"] == "Need approval"

    log_output = (run_dir / "run.log").read_text()
    first_line = log_output.splitlines()[0]
    assert re.match(
        r"^\d{4}-\d{2}-\d{2}T[0-9:.+-]+ \[loops\] signal accepted: NEEDS_INPUT$",
        first_line,
    )
    assert "signal accepted: NEEDS_INPUT" in log_output


def test_enqueue_state_signal_rejects_unsupported_state(tmp_path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with pytest.raises(ValueError, match="Unsupported state"):
        enqueue_state_signal(run_dir, state="DONE", message="nope")


def test_state_signal_cli_rejects_invalid_context(tmp_path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    env = os.environ.copy()
    env["LOOPS_RUN_DIR"] = str(run_dir)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "loops.state_signal",
            "--message",
            "Need help",
            "--context",
            "{not-json",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )

    assert result.returncode != 0
