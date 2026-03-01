from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from loops.approval_config import (
    DEFAULT_APPROVAL_COMMENT_PATTERN,
    normalize_approval_usernames,
)
from loops.handoff_handlers import DEFAULT_HANDOFF_HANDLER, validate_handoff_handler_name

INNER_LOOP_RUNTIME_CONFIG_FILE = "inner_loop_runtime_config.json"


@dataclass(frozen=True)
class InnerLoopRuntimeConfig:
    """Run-scoped inner-loop runtime settings materialized by the outer loop."""

    handoff_handler: str = DEFAULT_HANDOFF_HANDLER
    auto_approve_enabled: bool = False
    stream_logs_stdout: bool = False
    approval_comment_usernames: tuple[str, ...] = ()
    approval_comment_pattern: str = DEFAULT_APPROVAL_COMMENT_PATTERN
    review_actor_usernames: tuple[str, ...] = ()
    env: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "handoff_handler": self.handoff_handler,
            "auto_approve_enabled": self.auto_approve_enabled,
            "stream_logs_stdout": self.stream_logs_stdout,
            "approval_comment_usernames": list(self.approval_comment_usernames),
            "approval_comment_pattern": self.approval_comment_pattern,
            "review_actor_usernames": list(self.review_actor_usernames),
        }
        if self.env:
            payload["env"] = dict(sorted(self.env.items()))
        return payload


def write_inner_loop_runtime_config(
    run_dir: Path,
    config: InnerLoopRuntimeConfig,
) -> Path:
    """Persist run-scoped runtime settings for inner-loop execution."""

    target = run_dir / INNER_LOOP_RUNTIME_CONFIG_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(config.to_dict(), indent=2, sort_keys=True)
    fd = os.open(
        str(target),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)
    os.chmod(target, 0o600)
    return target


def read_inner_loop_runtime_config(run_dir: Path) -> Optional[InnerLoopRuntimeConfig]:
    """Load run-scoped runtime settings when present."""

    target = run_dir / INNER_LOOP_RUNTIME_CONFIG_FILE
    if not target.exists():
        return None

    payload = json.loads(target.read_text())
    if not isinstance(payload, Mapping):
        raise TypeError("inner loop runtime config must be an object")

    raw_handoff_handler = payload.get("handoff_handler", DEFAULT_HANDOFF_HANDLER)
    if not isinstance(raw_handoff_handler, str):
        raise TypeError("handoff_handler must be a string")
    handoff_handler = validate_handoff_handler_name(raw_handoff_handler)

    auto_approve_enabled = payload.get("auto_approve_enabled", False)
    if not isinstance(auto_approve_enabled, bool):
        raise TypeError("auto_approve_enabled must be a boolean")

    stream_logs_stdout = payload.get("stream_logs_stdout", False)
    if not isinstance(stream_logs_stdout, bool):
        raise TypeError("stream_logs_stdout must be a boolean")

    raw_approval_usernames = payload.get("approval_comment_usernames", ())
    if isinstance(raw_approval_usernames, tuple):
        approval_usernames_candidate = list(raw_approval_usernames)
    else:
        approval_usernames_candidate = raw_approval_usernames
    if not isinstance(approval_usernames_candidate, list) or not all(
        isinstance(item, str) for item in approval_usernames_candidate
    ):
        raise TypeError("approval_comment_usernames must be a list of strings")

    approval_comment_pattern = payload.get(
        "approval_comment_pattern",
        DEFAULT_APPROVAL_COMMENT_PATTERN,
    )
    if not isinstance(approval_comment_pattern, str):
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

    env_payload = payload.get("env")
    env: dict[str, str] | None = None
    if env_payload is not None:
        if not isinstance(env_payload, Mapping) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in env_payload.items()
        ):
            raise TypeError("env must be a string-to-string map")
        env = dict(env_payload)

    return InnerLoopRuntimeConfig(
        handoff_handler=handoff_handler,
        auto_approve_enabled=auto_approve_enabled,
        stream_logs_stdout=stream_logs_stdout,
        approval_comment_usernames=normalize_approval_usernames(
            approval_usernames_candidate
        ),
        approval_comment_pattern=(
            approval_comment_pattern or DEFAULT_APPROVAL_COMMENT_PATTERN
        ),
        review_actor_usernames=normalize_approval_usernames(review_usernames_candidate),
        env=env,
    )
