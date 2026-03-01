from __future__ import annotations

from contextvars import ContextVar, Token
import os
from datetime import datetime
from pathlib import Path
from typing import Mapping


STREAM_LOGS_STDOUT_ENV = "LOOPS_STREAM_LOGS_STDOUT"
LOG_TIMESTAMP_FRACTION_DIGITS = 2
_STREAM_LOGS_STDOUT_OVERRIDE: ContextVar[bool | None] = ContextVar(
    "loops_stream_logs_stdout_override",
    default=None,
)


def set_stream_logs_stdout_override(value: bool | None) -> Token[bool | None]:
    """Set a per-context override for log streaming behavior."""

    return _STREAM_LOGS_STDOUT_OVERRIDE.set(value)


def reset_stream_logs_stdout_override(token: Token[bool | None]) -> None:
    """Reset a previously set per-context log streaming override."""

    _STREAM_LOGS_STDOUT_OVERRIDE.reset(token)


def should_stream_logs_to_stdout(
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Return True when log lines should also be emitted to stdout."""

    override = _STREAM_LOGS_STDOUT_OVERRIDE.get()
    if override is not None:
        return override
    source = os.environ if environ is None else environ
    raw_value = source.get(STREAM_LOGS_STDOUT_ENV, "")
    return raw_value.strip().casefold() in {"1", "true", "yes", "on"}


def format_log_timestamp(
    *,
    now: datetime | None = None,
) -> str:
    """Render a local timestamp for log prefixes without timezone."""

    current = datetime.now() if now is None else now
    prefix = current.strftime("%Y-%m-%dT%H:%M:%S")
    fraction = f"{current.microsecond:06d}"[:LOG_TIMESTAMP_FRACTION_DIGITS]
    return f"{prefix}.{fraction}"


def append_log(path: Path, content: str) -> None:
    """Append content to a log file with local timestamp prefixes."""

    if not content:
        return
    timestamp = format_log_timestamp()
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered_lines = [f"{timestamp} {line}" for line in content.splitlines()]
    with path.open("a", encoding="utf-8") as handle:
        for line in rendered_lines:
            handle.write(f"{line}\n")

    if should_stream_logs_to_stdout():
        for line in rendered_lines:
            print(line, flush=True)
