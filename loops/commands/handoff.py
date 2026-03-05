from __future__ import annotations

from pathlib import Path
from typing import Optional

import click


@click.command("handoff")
@click.argument("session_id", required=False)
@click.option(
    "--config",
    "config_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=Path(".loops/config.json"),
    show_default=True,
    help="Path to the loops config JSON.",
)
@click.option(
    "--pr-url",
    "pr_url",
    type=str,
    default=None,
    help="Optional PR URL override when session discovery is ambiguous.",
)
@click.option(
    "--task-url",
    "task_url",
    type=str,
    default=None,
    help="Optional tracking task URL override when session discovery is ambiguous.",
)
def handoff_command(
    session_id: Optional[str],
    config_path: Path,
    pr_url: Optional[str],
    task_url: Optional[str],
) -> None:
    """Handoff an existing Codex session into a Loops WAITING_ON_REVIEW run."""

    from loops.core import cli as cli_module

    cli_module._run_handoff_command(
        config_path=config_path,
        session_id=session_id,
        pr_url=pr_url,
        task_url=task_url,
    )
