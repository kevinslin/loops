from __future__ import annotations

from pathlib import Path

import click


@click.command("init")
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

    from loops.core import cli as cli_module

    cli_module._run_init_command(loops_root=loops_root, force=force)
