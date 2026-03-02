from __future__ import annotations

"""Top-level command-line interface for Loops."""

from dataclasses import replace
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any, Optional

import click

from loops.core.inner_loop import reset_run_record, run_inner_loop
from loops.core.outer_loop import (
    InnerLoopCommandConfig,
    OuterLoopRunner,
    OuterLoopState,
    SyncModeInterruptedError,
    _resolve_provider_comment_approval_pattern,
    _resolve_provider_comment_approval_usernames,
    _resolve_provider_review_actor_usernames,
    build_default_loop_config_payload,
    build_inner_loop_launcher,
    build_provider,
    load_config,
    upgrade_config_payload,
    write_outer_state,
)
from loops.state.constants import (
    INNER_LOOP_RUNS_DIR_NAME,
    LATEST_LOOPS_CONFIG_VERSION,
    OUTER_LOG_FILE_NAME,
    OUTER_STATE_FILE_NAME,
    RUN_RECORD_FILE_NAME,
)
from loops.task_providers.github_projects_v2 import (
    GITHUB_PROJECTS_V2_PROVIDER_ID,
    build_default_provider_config_payload,
)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def main() -> None:
    """Loops CLI wrapper for setup, outer loop, and inner loop."""


def _run_outer_loop(
    *,
    config_path: Path,
    run_once: bool,
    limit: Optional[int],
    force: Optional[bool],
    task_url: Optional[str] = None,
) -> None:
    """Run the configured outer loop."""

    config = load_config(config_path)
    effective_loop_config = config.loop_config
    force_override: Optional[bool]
    if task_url is not None:
        force_override = True
        effective_loop_config = replace(effective_loop_config, sync_mode=True)
    else:
        force_override = force
    if force_override is not None:
        effective_loop_config = replace(effective_loop_config, force=force_override)
    config = replace(config, loop_config=effective_loop_config)
    if config.inner_loop is None:
        config = replace(config, inner_loop=_build_default_inner_loop_config())
    provider = build_provider(config)
    launcher = build_inner_loop_launcher(
        config,
        approval_comment_usernames=_resolve_provider_comment_approval_usernames(
            provider
        ),
        approval_comment_pattern=_resolve_provider_comment_approval_pattern(provider),
        review_actor_usernames=_resolve_provider_review_actor_usernames(provider),
    )
    loops_root = _resolve_loops_root(config_path)
    runner = OuterLoopRunner(
        provider,
        config.loop_config,
        loops_root=loops_root,
        inner_loop_launcher=launcher,
    )
    effective_run_once = run_once or task_url is not None
    try:
        if effective_run_once:
            runner.run_once(limit=limit, forced_task_url=task_url)
        else:
            runner.run_forever(limit=limit)
    except KeyboardInterrupt as exc:
        if isinstance(exc, SyncModeInterruptedError):
            _print_sync_resume_instructions(run_dir=exc.run_dir)
        raise click.Abort() from exc


def _run_inner_loop_command(
    *,
    run_dir: Optional[Path],
    prompt_file: Optional[Path],
    reset: bool,
) -> None:
    """Run the inner loop command implementation."""

    resolved_run_dir = _resolve_run_dir_option(run_dir)
    try:
        if reset:
            reset_run_record(resolved_run_dir)
            click.echo(f"Reset run state: {resolved_run_dir / RUN_RECORD_FILE_NAME}")
            return
        run_inner_loop(resolved_run_dir, prompt_file=prompt_file)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc


def _run_init_command(*, loops_root: Path, force: bool) -> None:
    """Run the init command implementation."""

    loops_root = loops_root.resolve()
    loops_root.mkdir(parents=True, exist_ok=True)

    config_path = loops_root / "config.json"
    if config_path.exists() and not force:
        raise click.ClickException(
            f"Config already exists: {config_path} (re-run with --force to overwrite)"
        )

    state_path = loops_root / OUTER_STATE_FILE_NAME
    if not state_path.exists():
        write_outer_state(state_path, OuterLoopState.empty())
    (loops_root / OUTER_LOG_FILE_NAME).touch(exist_ok=True)
    (loops_root / INNER_LOOP_RUNS_DIR_NAME).mkdir(parents=True, exist_ok=True)

    config_path.write_text(
        json.dumps(_build_default_config(), indent=2, sort_keys=True) + "\n"
    )
    click.echo(f"Initialized Loops in {loops_root}")
    click.echo(f"Config: {config_path}")


def _run_doctor_command(*, config_path: Path) -> None:
    """Run the doctor command implementation."""

    resolved_path = config_path.resolve()
    if not resolved_path.exists():
        raise click.ClickException(f"Config does not exist: {resolved_path}")

    try:
        payload = json.loads(resolved_path.read_text())
        upgraded_payload, changed = upgrade_config_payload(payload)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise click.ClickException(str(exc)) from exc

    if changed:
        resolved_path.write_text(
            json.dumps(upgraded_payload, indent=2, sort_keys=True) + "\n"
        )
        click.echo(
            f"Upgraded config to version {LATEST_LOOPS_CONFIG_VERSION}: {resolved_path}"
        )
        return

    click.echo(
        f"Config already up to date (version {LATEST_LOOPS_CONFIG_VERSION}): {resolved_path}"
    )


def _print_sync_resume_instructions(*, run_dir: Path) -> None:
    """Print instructions for resuming an interrupted sync-mode inner loop."""

    click.echo(
        "Sync mode interrupted. Resume this run with:\n"
        f"loops inner-loop --run-dir {shlex.quote(str(run_dir))}"
    )


def _resolve_run_dir_option(run_dir: Optional[Path]) -> Path:
    """Resolve --run-dir with LOOPS_RUN_DIR fallback."""

    if run_dir is not None:
        return run_dir
    env_run_dir = os.environ.get("LOOPS_RUN_DIR")
    if env_run_dir:
        return Path(env_run_dir)
    raise click.ClickException("LOOPS_RUN_DIR is required (or pass --run-dir)")


def _build_default_config() -> dict[str, Any]:
    """Build a default Loops config payload for `loops init`."""

    return {
        "version": LATEST_LOOPS_CONFIG_VERSION,
        "task_provider_id": GITHUB_PROJECTS_V2_PROVIDER_ID,
        "task_provider_config": build_default_provider_config_payload(),
        "loop_config": build_default_loop_config_payload(),
        "inner_loop": _build_default_inner_loop_payload(),
    }


def _build_default_inner_loop_config() -> InnerLoopCommandConfig:
    """Build the default runtime fallback InnerLoopCommandConfig."""

    return InnerLoopCommandConfig(
        command=[sys.executable, "-m", "loops.inner_loop"],
        append_task_url=False,
    )


def _build_default_inner_loop_payload() -> dict[str, Any]:
    """Build the default JSON payload for `inner_loop` config."""

    defaults = _build_default_inner_loop_config()
    return {
        "command": defaults.command,
        "append_task_url": defaults.append_task_url,
    }


def _resolve_loops_root(config_path: Path) -> Path:
    """Resolve the loops root directory based on the config path."""

    resolved = config_path.resolve()
    if resolved.parent.name == ".loops":
        return resolved.parent
    return resolved.parent / ".loops"

# Import subcommands at the end to avoid circular imports: command modules
# call helpers from this module.
from loops.commands.clean import clean_command as clean_command
from loops.commands.doctor import doctor_command as doctor_command
from loops.commands.init import init_command as init_command
from loops.commands.inner_loop import inner_loop_command as inner_loop_command
from loops.commands.run import run_command as run_command

main.add_command(run_command)
main.add_command(inner_loop_command)
main.add_command(init_command)
main.add_command(doctor_command)
main.add_command(clean_command)


if __name__ == "__main__":
    main(prog_name="loops")
