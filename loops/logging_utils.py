from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping


STREAM_LOGS_STDOUT_ENV = "LOOPS_STREAM_LOGS_STDOUT"


def should_stream_logs_to_stdout(
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Return True when log lines should also be emitted to stdout."""

    source = os.environ if environ is None else environ
    raw_value = source.get(STREAM_LOGS_STDOUT_ENV, "")
    return raw_value.strip().casefold() in {"1", "true", "yes", "on"}


def append_log(path: Path, content: str) -> None:
    """Append content to a log file with ISO UTC timestamp prefixes."""

    if not content:
        return
    timestamp = datetime.now(timezone.utc).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered_lines = [f"{timestamp} {line}" for line in content.splitlines()]
    with path.open("a", encoding="utf-8") as handle:
        for line in rendered_lines:
            handle.write(f"{line}\n")

    if should_stream_logs_to_stdout():
        for line in rendered_lines:
            print(line, flush=True)
