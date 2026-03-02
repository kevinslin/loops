from __future__ import annotations

from pathlib import Path
from typing import Optional

import click


@click.command("run")
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

    from loops.core import cli as cli_module

    cli_module._run_outer_loop(
        config_path=config_path,
        run_once=run_once,
        limit=limit,
        force=force,
        task_url=task_url,
    )
