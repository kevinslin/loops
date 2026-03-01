from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

DEFAULT_APPROVAL_COMMENT_PATTERN = r"^\s*/approve\b"
INNER_LOOP_APPROVAL_CONFIG_FILE = "inner_loop_approval_config.json"


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


@dataclass(frozen=True)
class InnerLoopApprovalConfig:
    approval_comment_usernames: tuple[str, ...] = ()
    approval_comment_pattern: str = DEFAULT_APPROVAL_COMMENT_PATTERN
    review_actor_usernames: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_comment_usernames": list(self.approval_comment_usernames),
            "approval_comment_pattern": self.approval_comment_pattern,
            "review_actor_usernames": list(self.review_actor_usernames),
        }


def build_inner_loop_approval_config(
    *,
    approval_comment_usernames: Iterable[str],
    approval_comment_pattern: str,
    review_actor_usernames: Iterable[str] = (),
) -> InnerLoopApprovalConfig:
    return InnerLoopApprovalConfig(
        approval_comment_usernames=normalize_approval_usernames(
            approval_comment_usernames
        ),
        approval_comment_pattern=approval_comment_pattern or DEFAULT_APPROVAL_COMMENT_PATTERN,
        review_actor_usernames=normalize_approval_usernames(review_actor_usernames),
    )


def write_inner_loop_approval_config(
    run_dir: Path,
    config: InnerLoopApprovalConfig,
) -> Path:
    """Persist run-scoped approval configuration for inner-loop polling."""

    target = run_dir / INNER_LOOP_APPROVAL_CONFIG_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(config.to_dict(), indent=2, sort_keys=True))
    return target


def read_inner_loop_approval_config(run_dir: Path) -> InnerLoopApprovalConfig:
    """Load run-scoped approval configuration with validation."""

    target = run_dir / INNER_LOOP_APPROVAL_CONFIG_FILE
    if not target.exists():
        return InnerLoopApprovalConfig()
    payload = json.loads(target.read_text())
    if not isinstance(payload, Mapping):
        raise TypeError("inner loop approval config must be an object")
    raw_usernames = payload.get("approval_comment_usernames", ())
    if isinstance(raw_usernames, tuple):
        usernames_candidate = list(raw_usernames)
    else:
        usernames_candidate = raw_usernames
    if not isinstance(usernames_candidate, list) or not all(
        isinstance(item, str) for item in usernames_candidate
    ):
        raise TypeError("approval_comment_usernames must be a list of strings")
    pattern = payload.get("approval_comment_pattern", DEFAULT_APPROVAL_COMMENT_PATTERN)
    if not isinstance(pattern, str):
        raise TypeError("approval_comment_pattern must be a string")
    raw_review_usernames = payload.get("review_actor_usernames", ())
    if isinstance(raw_review_usernames, tuple):
        review_usernames_candidate = list(raw_review_usernames)
    else:
        review_usernames_candidate = raw_review_usernames
    if not isinstance(review_usernames_candidate, list) or not all(
        isinstance(item, str) for item in review_usernames_candidate
    ):
        raise TypeError("review_actor_usernames must be a list of strings")
    return InnerLoopApprovalConfig(
        approval_comment_usernames=normalize_approval_usernames(usernames_candidate),
        approval_comment_pattern=pattern or DEFAULT_APPROVAL_COMMENT_PATTERN,
        review_actor_usernames=normalize_approval_usernames(review_usernames_candidate),
    )
