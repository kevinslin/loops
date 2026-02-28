from __future__ import annotations

"""Signal channel for requesting inner-loop state transitions."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loops.logging_utils import append_log

SUPPORTED_SIGNAL_STATES = {"NEEDS_INPUT"}
SIGNAL_QUEUE_FILE = "state_signals.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def enqueue_state_signal(
    run_dir: Path,
    *,
    state: str,
    message: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state = state.strip().upper()
    if state not in SUPPORTED_SIGNAL_STATES:
        supported = ", ".join(sorted(SUPPORTED_SIGNAL_STATES))
        raise ValueError(f"Unsupported state '{state}'. Supported states: {supported}")
    normalized_message = message.strip()
    if not normalized_message:
        raise ValueError("message must be non-empty")

    run_dir = run_dir.resolve()
    queue_path = run_dir / SIGNAL_QUEUE_FILE
    payload: dict[str, Any] = {"message": normalized_message}
    if context:
        payload["context"] = context
    signal = {"state": state, "payload": payload, "created_at": _now_iso()}
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    with queue_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(signal, ensure_ascii=True, sort_keys=True))
        handle.write("\n")

    append_log(run_dir / "run.log", f"[loops] signal accepted: {state}")
    return signal


def main() -> None:
    from loops.cli import main as cli_main

    args = sys.argv[1:]
    cli_main.main(
        args=["signal", *args],
        prog_name="loops",
        standalone_mode=True,
    )


if __name__ == "__main__":
    main()
