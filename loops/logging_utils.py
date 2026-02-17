from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


def append_log(path: Path, content: str) -> None:
    """Append content to a log file with ISO UTC timestamp prefixes."""

    if not content:
        return
    timestamp = datetime.now(timezone.utc).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for line in content.splitlines():
            handle.write(f"{timestamp} {line}\n")
