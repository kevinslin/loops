from __future__ import annotations

from pathlib import Path
from typing import Optional

import click


@click.command("inner-loop")
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

    from loops.core import cli as cli_module

    cli_module._run_inner_loop_command(
        run_dir=run_dir,
        prompt_file=prompt_file,
        reset=reset,
    )
