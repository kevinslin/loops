from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from loops.handoff_handlers import DEFAULT_HANDOFF_HANDLER, validate_handoff_handler_name

INNER_LOOP_RUNTIME_CONFIG_FILE = "inner_loop_runtime_config.json"


@dataclass(frozen=True)
class InnerLoopRuntimeConfig:
    """Run-scoped inner-loop runtime settings materialized by the outer loop."""

    handoff_handler: str = DEFAULT_HANDOFF_HANDLER
    auto_approve_enabled: bool = False
    stream_logs_stdout: bool = False
    env: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "handoff_handler": self.handoff_handler,
            "auto_approve_enabled": self.auto_approve_enabled,
            "stream_logs_stdout": self.stream_logs_stdout,
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
    target.write_text(json.dumps(config.to_dict(), indent=2, sort_keys=True))
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
        env=env,
    )
