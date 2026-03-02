from __future__ import annotations

from pathlib import Path

import click


@click.command("doctor")
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

    from loops.core import cli as cli_module

    cli_module._run_doctor_command(config_path=config_path)
