from __future__ import annotations

"""Config types and parsing utilities for Loops."""

from dataclasses import dataclass
import json
import shlex
from pathlib import Path
from typing import Any, Optional

from loops.approval_config import (
    DEFAULT_APPROVAL_COMMENT_PATTERN,
    normalize_approval_usernames,
)
from loops.handoff_handlers import (
    DEFAULT_HANDOFF_HANDLER,
    validate_handoff_handler_name,
    validate_handoff_handler_provider_compatibility,
)
from loops.providers.github_projects_v2 import (
    GITHUB_PROJECTS_V2_PROVIDER_ID,
    build_default_provider_config_payload,
)

LATEST_LOOPS_CONFIG_VERSION = 2


@dataclass(frozen=True)
class OuterLoopConfig:
    """Configuration for the outer loop polling and dispatch behavior."""

    poll_interval_seconds: int = 30
    parallel_tasks: bool = False
    parallel_tasks_limit: int = 5
    sync_mode: bool = False
    emit_on_first_run: bool = False
    force: bool = False
    task_ready_status: str = "Ready"
    approval_comment_usernames: tuple[str, ...] = ()
    approval_comment_pattern: str = DEFAULT_APPROVAL_COMMENT_PATTERN
    auto_approve_enabled: bool = False
    handoff_handler: str = DEFAULT_HANDOFF_HANDLER


@dataclass(frozen=True)
class InnerLoopCommandConfig:
    """Configuration for launching inner loop commands."""

    command: list[str]
    working_dir: Optional[str] = None
    env: Optional[dict[str, str]] = None
    append_task_url: bool = True

    @staticmethod
    def from_dict(
        payload: dict[str, Any],
        *,
        base_dir: Optional[Path] = None,
    ) -> "InnerLoopCommandConfig":
        """Build an InnerLoopCommandConfig from a dict payload."""

        if "command" not in payload:
            raise KeyError("inner_loop.command is required")
        raw_command = payload["command"]
        if isinstance(raw_command, str):
            command = shlex.split(raw_command)
        elif isinstance(raw_command, list) and all(
            isinstance(item, str) for item in raw_command
        ):
            command = list(raw_command)
        else:
            raise TypeError("inner_loop.command must be a string or list of strings")
        if not command:
            raise ValueError("inner_loop.command cannot be empty")
        working_dir = payload.get("working_dir")
        if working_dir is not None and not isinstance(working_dir, str):
            raise TypeError("inner_loop.working_dir must be a string")
        if working_dir is not None and base_dir is not None:
            working_path = Path(working_dir)
            if not working_path.is_absolute():
                working_dir = str((base_dir / working_path).resolve())
        env = payload.get("env")
        if env is not None:
            if not isinstance(env, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in env.items()
            ):
                raise TypeError("inner_loop.env must be a string-to-string map")
        append_task_url = payload.get("append_task_url", True)
        if not isinstance(append_task_url, bool):
            raise TypeError("inner_loop.append_task_url must be a boolean")
        return InnerLoopCommandConfig(
            command=command,
            working_dir=working_dir,
            env=env,
            append_task_url=append_task_url,
        )


@dataclass(frozen=True)
class LoopsConfig:
    """Top-level Loops configuration loaded from JSON."""

    version: int
    task_provider_id: str
    task_provider_config: dict[str, Any]
    loop_config: OuterLoopConfig
    inner_loop: Optional[InnerLoopCommandConfig] = None


def load_config(path: str | Path) -> LoopsConfig:
    """Load a LoopsConfig from a JSON file."""

    payload = json.loads(Path(path).read_text())
    payload, _ = upgrade_config_payload(payload)
    task_provider_id = payload.get("task_provider_id")
    if not isinstance(task_provider_id, str) or not task_provider_id:
        raise TypeError("task_provider_id is required and must be a string")
    task_provider_config = payload.get("task_provider_config")
    if not isinstance(task_provider_config, dict):
        raise TypeError("task_provider_config must be an object")
    loop_config = _load_outer_loop_config(payload.get("loop_config"))
    validate_handoff_handler_provider_compatibility(
        loop_config.handoff_handler,
        task_provider_id,
    )
    inner_loop_payload = payload.get("inner_loop")
    inner_loop = None
    if inner_loop_payload is not None:
        if not isinstance(inner_loop_payload, dict):
            raise TypeError("inner_loop must be an object")
        base_dir = Path(path).resolve().parent
        inner_loop = InnerLoopCommandConfig.from_dict(
            inner_loop_payload,
            base_dir=base_dir,
        )
    return LoopsConfig(
        version=_load_config_version(payload),
        task_provider_id=task_provider_id,
        task_provider_config=task_provider_config,
        loop_config=loop_config,
        inner_loop=inner_loop,
    )


def upgrade_config_payload(payload: Any) -> tuple[dict[str, Any], bool]:
    """Upgrade a config payload in-memory to the latest schema version."""

    if not isinstance(payload, dict):
        raise TypeError("Config must be a JSON object")
    upgraded = dict(payload)
    changed = False
    version = _load_config_version(upgraded)

    if version > LATEST_LOOPS_CONFIG_VERSION:
        raise ValueError(
            f"Unsupported config version: {version}. "
            f"Latest supported version is {LATEST_LOOPS_CONFIG_VERSION}."
        )

    if version < LATEST_LOOPS_CONFIG_VERSION:
        upgraded["version"] = LATEST_LOOPS_CONFIG_VERSION
        changed = True

    legacy_provider_id = upgraded.pop("provider_id", None)
    if legacy_provider_id is not None:
        changed = True
        if "task_provider_id" not in upgraded:
            upgraded["task_provider_id"] = legacy_provider_id

    legacy_provider_config = upgraded.pop("provider_config", None)
    if legacy_provider_config is not None:
        changed = True
        if "task_provider_config" not in upgraded:
            upgraded["task_provider_config"] = legacy_provider_config

    existing_loop_payload = upgraded.get("loop_config")
    if existing_loop_payload is None:
        loop_payload: dict[str, Any] = {}
    elif isinstance(existing_loop_payload, dict):
        loop_payload = dict(existing_loop_payload)
    else:
        raise TypeError("loop_config must be an object")

    for key, value in build_default_loop_config_payload().items():
        if key not in loop_payload:
            loop_payload[key] = value
            changed = True

    if existing_loop_payload != loop_payload:
        upgraded["loop_config"] = loop_payload

    task_provider_id = upgraded.get("task_provider_id")
    if task_provider_id == GITHUB_PROJECTS_V2_PROVIDER_ID:
        existing_provider_payload = upgraded.get("task_provider_config")
        if existing_provider_payload is None:
            provider_payload: dict[str, Any] = {}
        elif isinstance(existing_provider_payload, dict):
            provider_payload = dict(existing_provider_payload)
        else:
            raise TypeError("task_provider_config must be an object")
        project_url = provider_payload.get("url")
        if not isinstance(project_url, str) or not project_url.strip():
            provider_defaults = build_default_provider_config_payload()
        else:
            provider_defaults = build_default_provider_config_payload(
                project_url=project_url,
            )
        for key, value in provider_defaults.items():
            if key == "url":
                continue
            if key not in provider_payload:
                provider_payload[key] = value
                changed = True
        if existing_provider_payload != provider_payload:
            upgraded["task_provider_config"] = provider_payload

    return upgraded, changed


def build_default_loop_config_payload() -> dict[str, Any]:
    """Build the canonical loop_config defaults payload for JSON config files."""

    defaults = OuterLoopConfig()
    return {
        "poll_interval_seconds": defaults.poll_interval_seconds,
        "parallel_tasks": defaults.parallel_tasks,
        "parallel_tasks_limit": defaults.parallel_tasks_limit,
        "sync_mode": defaults.sync_mode,
        "emit_on_first_run": defaults.emit_on_first_run,
        "force": defaults.force,
        "task_ready_status": defaults.task_ready_status,
        "approval_comment_usernames": list(defaults.approval_comment_usernames),
        "approval_comment_pattern": defaults.approval_comment_pattern,
        "auto_approve_enabled": defaults.auto_approve_enabled,
        "handoff_handler": defaults.handoff_handler,
    }


def _load_outer_loop_config(payload: Any) -> OuterLoopConfig:
    """Load outer loop configuration from JSON payload."""

    if payload is None:
        raw_payload: dict[str, Any] = {}
    elif isinstance(payload, dict):
        raw_payload = payload
    else:
        raise TypeError("loop_config must be an object")
    defaults = build_default_loop_config_payload()
    merged_payload = {**defaults, **raw_payload}
    poll_interval = _load_int(
        merged_payload,
        "poll_interval_seconds",
        defaults["poll_interval_seconds"],
    )
    if poll_interval <= 0:
        raise ValueError("poll_interval_seconds must be positive")
    parallel_limit = _load_int(
        merged_payload,
        "parallel_tasks_limit",
        defaults["parallel_tasks_limit"],
    )
    if parallel_limit <= 0:
        raise ValueError("parallel_tasks_limit must be positive")
    return OuterLoopConfig(
        poll_interval_seconds=poll_interval,
        parallel_tasks=_load_bool(
            merged_payload,
            "parallel_tasks",
            defaults["parallel_tasks"],
        ),
        parallel_tasks_limit=parallel_limit,
        sync_mode=_load_bool(merged_payload, "sync_mode", defaults["sync_mode"]),
        emit_on_first_run=_load_bool(
            merged_payload,
            "emit_on_first_run",
            defaults["emit_on_first_run"],
        ),
        force=_load_bool(merged_payload, "force", defaults["force"]),
        task_ready_status=_load_str(
            merged_payload,
            "task_ready_status",
            defaults["task_ready_status"],
        ),
        approval_comment_usernames=normalize_approval_usernames(
            _load_str_list(
                merged_payload,
                "approval_comment_usernames",
                tuple(defaults["approval_comment_usernames"]),
            )
        ),
        approval_comment_pattern=_load_str(
            merged_payload,
            "approval_comment_pattern",
            defaults["approval_comment_pattern"],
        ),
        auto_approve_enabled=_load_bool(
            merged_payload,
            "auto_approve_enabled",
            defaults["auto_approve_enabled"],
        ),
        handoff_handler=validate_handoff_handler_name(
            _load_str(
                merged_payload,
                "handoff_handler",
                defaults["handoff_handler"],
            )
        ),
    )


def _load_bool(payload: dict[str, Any], key: str, default: bool) -> bool:
    """Load a boolean config value with validation."""

    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise TypeError(f"{key} must be a boolean")
    return value


def _load_int(payload: dict[str, Any], key: str, default: int) -> int:
    """Load an integer config value with validation."""

    value = payload.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{key} must be an integer")
    return value


def _load_str(payload: dict[str, Any], key: str, default: str) -> str:
    """Load a string config value with validation."""

    value = payload.get(key, default)
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string")
    return value


def _load_str_list(
    payload: dict[str, Any],
    key: str,
    default: tuple[str, ...],
) -> tuple[str, ...]:
    """Load a list of strings with validation."""

    value = payload.get(key, default)
    if isinstance(value, tuple):
        candidate = list(value)
    else:
        candidate = value
    if not isinstance(candidate, list) or not all(
        isinstance(item, str) for item in candidate
    ):
        raise TypeError(f"{key} must be a list of strings")
    return tuple(candidate)


def _load_config_version(payload: dict[str, Any]) -> int:
    value = payload.get("version", 0)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("version must be an integer")
    if value < 0:
        raise ValueError("version must be >= 0")
    return value

