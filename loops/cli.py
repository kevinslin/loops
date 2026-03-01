from __future__ import annotations

"""Top-level command-line interface for Loops."""

from dataclasses import replace
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any
from typing import Optional

import click

from loops.inner_loop import reset_run_record, run_inner_loop
from loops.outer_loop import (
    InnerLoopCommandConfig,
    INNER_LOOP_RUNS_DIR_NAME,
    LATEST_LOOPS_CONFIG_VERSION,
    OuterLoopRunner,
    OuterLoopState,
    SyncModeInterruptedError,
    build_default_loop_config_payload,
    build_inner_loop_launcher,
    build_provider,
    load_config,
    upgrade_config_payload,
    write_outer_state,
)
from loops.providers.github_projects_v2 import (
    GITHUB_PROJECTS_V2_PROVIDER_ID,
    build_default_provider_config_payload,
)
from loops.state_signal import enqueue_state_signal


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def main() -> None:
    """Loops CLI wrapper for setup, outer loop, inner loop, and signals."""


@main.command("run")
@click.option(
    "--config",
    "config_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=Path(".loops/config.json"),
    show_default=True,
    help="Path to the loops config JSON.",
)
@click.option(
    "--run-once/--run-forever",
    default=False,
    help="Run a single poll cycle and exit.",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Optional provider poll limit.",
)
@click.option(
    "--force/--no-force",
    default=None,
    help="Override the force flag in config.",
)
@click.option(
    "--task-url",
    "task_url",
    type=str,
    default=None,
    help="Force processing a specific task URL from provider results (implies --run-once, --force, and sync_mode=true).",
)
def run_command(
    config_path: Path,
    run_once: bool,
    limit: Optional[int],
    force: Optional[bool],
    task_url: Optional[str],
) -> None:
    """Run the outer loop runner using the provided config."""

    _run_outer_loop(
        config_path=config_path,
        run_once=run_once,
        limit=limit,
        force=force,
        task_url=task_url,
    )


@main.command("inner-loop")
@click.option(
    "--run-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Path to the run directory (defaults to LOOPS_RUN_DIR).",
)
@click.option(
    "--prompt-file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Optional path to a base prompt file.",
)
@click.option(
    "--reset",
    is_flag=True,
    default=False,
    help="Reset run.json to initial state and exit.",
)
def inner_loop_command(
    run_dir: Optional[Path],
    prompt_file: Optional[Path],
    reset: bool,
) -> None:
    """Run the inner loop for a specific run directory."""

    resolved_run_dir = _resolve_run_dir_option(run_dir)
    try:
        if reset:
            reset_run_record(resolved_run_dir)
            click.echo(f"Reset run state: {resolved_run_dir / 'run.json'}")
            return
        run_inner_loop(resolved_run_dir, prompt_file=prompt_file)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc


@main.command("signal")
@click.option(
    "--run-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Path to run directory (defaults to LOOPS_RUN_DIR).",
)
@click.option(
    "--state",
    type=str,
    default="NEEDS_INPUT",
    show_default=True,
    help="Signal state to enqueue.",
)
@click.option(
    "--message",
    type=str,
    required=True,
    help="Prompt message to show when user input is required.",
)
@click.option(
    "--context",
    type=str,
    default="",
    help="Optional JSON object context for the signal payload.",
)
def signal_command(
    run_dir: Optional[Path],
    state: str,
    message: str,
    context: str,
) -> None:
    """Enqueue a state signal for an existing run directory."""

    resolved_run_dir = _resolve_run_dir_option(run_dir)
    parsed_context = _parse_context_option(context)
    try:
        signal = enqueue_state_signal(
            resolved_run_dir,
            state=state,
            message=message,
            context=parsed_context,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps({"accepted": True, "signal": signal}, ensure_ascii=True))


@main.command("init")
@click.option(
    "--loops-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path(".loops"),
    show_default=True,
    help="Directory where Loops runtime state and config are stored.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Overwrite an existing config.json file.",
)
def init_command(loops_root: Path, force: bool) -> None:
    """Initialize the default .loops directory structure and config."""

    loops_root = loops_root.resolve()
    loops_root.mkdir(parents=True, exist_ok=True)

    config_path = loops_root / "config.json"
    if config_path.exists() and not force:
        raise click.ClickException(
            f"Config already exists: {config_path} (re-run with --force to overwrite)"
        )

    state_path = loops_root / "outer_state.json"
    if not state_path.exists():
        write_outer_state(state_path, OuterLoopState.empty())
    (loops_root / "oloops.log").touch(exist_ok=True)
    (loops_root / INNER_LOOP_RUNS_DIR_NAME).mkdir(parents=True, exist_ok=True)

    config_path.write_text(
        json.dumps(_build_default_config(), indent=2, sort_keys=True) + "\n"
    )
    click.echo(f"Initialized Loops in {loops_root}")
    click.echo(f"Config: {config_path}")


@main.command("doctor")
@click.option(
    "--config",
    "config_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=Path(".loops/config.json"),
    show_default=True,
    help="Path to the loops config JSON.",
)
def doctor_command(config_path: Path) -> None:
    """Upgrade config.json to the latest schema version and defaults."""

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
    launcher = build_inner_loop_launcher(config)
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
        if config.loop_config.sync_mode:
            interrupted_run_dir: Path | None = None
            if isinstance(exc, SyncModeInterruptedError):
                interrupted_run_dir = exc.run_dir
            _print_sync_resume_instructions(
                loops_root=loops_root,
                run_dir=interrupted_run_dir,
            )
        raise click.Abort() from exc


def _print_sync_resume_instructions(*, loops_root: Path, run_dir: Path | None) -> None:
    """Print instructions for resuming an interrupted sync-mode inner loop."""

    if run_dir is not None:
        click.echo(
            "Sync mode interrupted. Resume this run with:\n"
            f"loops inner-loop --run-dir {shlex.quote(str(run_dir))}"
        )
        return
    jobs_root = loops_root / INNER_LOOP_RUNS_DIR_NAME
    click.echo(
        "Sync mode interrupted. Resume the latest run with:\n"
        "loops inner-loop --run-dir <RUN_DIR>\n"
        f"Run directories are under: {jobs_root}"
    )


def _resolve_run_dir_option(run_dir: Optional[Path]) -> Path:
    """Resolve --run-dir with LOOPS_RUN_DIR fallback."""

    if run_dir is not None:
        return run_dir
    env_run_dir = os.environ.get("LOOPS_RUN_DIR")
    if env_run_dir:
        return Path(env_run_dir)
    raise click.ClickException("LOOPS_RUN_DIR is required (or pass --run-dir)")


def _parse_context_option(raw_context: str) -> dict[str, Any]:
    """Parse --context JSON into an object."""

    if not raw_context.strip():
        return {}
    try:
        parsed = json.loads(raw_context)
    except json.JSONDecodeError as exc:
        raise click.ClickException("--context must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise click.ClickException("--context JSON must be an object")
    return parsed


def _build_default_config() -> dict[str, Any]:
    """Build a default Loops config payload for `loops init`."""

    return {
        "version": LATEST_LOOPS_CONFIG_VERSION,
        "provider_id": GITHUB_PROJECTS_V2_PROVIDER_ID,
        "provider_config": build_default_provider_config_payload(),
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


if __name__ == "__main__":
    main(prog_name="loops")
