from __future__ import annotations

"""Signal channel for requesting inner-loop state transitions."""

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SUPPORTED_SIGNAL_STATES = {"NEEDS_INPUT"}
SIGNAL_QUEUE_FILE = "state_signals.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_run_dir(run_dir: str | None) -> Path:
    if run_dir:
        return Path(run_dir)
    env_run_dir = os.environ.get("LOOPS_RUN_DIR")
    if env_run_dir:
        return Path(env_run_dir)
    raise SystemExit("LOOPS_RUN_DIR is required (or pass --run-dir)")


def _append_log(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(content)
        if not content.endswith("\n"):
            handle.write("\n")


def _parse_context(raw_context: str) -> dict[str, Any]:
    if not raw_context.strip():
        return {}
    try:
        parsed = json.loads(raw_context)
    except json.JSONDecodeError as exc:
        raise ValueError("--context must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("--context JSON must be an object")
    return parsed


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

    _append_log(run_dir / "run.log", f"[loops] signal accepted: {state}")
    return signal


def main() -> None:
    parser = argparse.ArgumentParser(description="Enqueue an inner-loop state signal.")
    parser.add_argument(
        "--run-dir",
        type=str,
        default=None,
        help="Path to run directory (defaults to LOOPS_RUN_DIR).",
    )
    parser.add_argument(
        "--state",
        type=str,
        default="NEEDS_INPUT",
        help="Signal state to enqueue.",
    )
    parser.add_argument(
        "--message",
        type=str,
        required=True,
        help="Prompt message to show when user input is required.",
    )
    parser.add_argument(
        "--context",
        type=str,
        default="",
        help="Optional JSON object context for the signal payload.",
    )
    args = parser.parse_args()

    run_dir = _resolve_run_dir(args.run_dir)
    context = _parse_context(args.context)
    signal = enqueue_state_signal(
        run_dir,
        state=args.state,
        message=args.message,
        context=context,
    )
    print(json.dumps({"accepted": True, "signal": signal}, ensure_ascii=True))


if __name__ == "__main__":
    main()
