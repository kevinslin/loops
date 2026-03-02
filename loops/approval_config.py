from __future__ import annotations

from collections.abc import Iterable

DEFAULT_APPROVAL_COMMENT_PATTERN = r"^\s*/approve\b"


def normalize_approval_usernames(usernames: Iterable[str]) -> tuple[str, ...]:
    """Normalize usernames for case-insensitive allowlist matching."""

    normalized: list[str] = []
    seen: set[str] = set()
    for value in usernames:
        candidate = value.strip().casefold()
        if not candidate or candidate in seen:
            continue
        normalized.append(candidate)
        seen.add(candidate)
    return tuple(normalized)
