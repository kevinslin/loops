from __future__ import annotations

"""Command-line interface for the Loops outer loop runner."""

from dataclasses import replace
from pathlib import Path
from typing import Optional

import click

from loops.outer_loop import (
    OuterLoopRunner,
    build_inner_loop_launcher,
    build_provider,
    load_config,
)


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
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
def main(
    config_path: Path,
    run_once: bool,
    limit: Optional[int],
    force: Optional[bool],
) -> None:
    """Run the outer loop runner using the provided config."""

    config = load_config(config_path)
    loop_config = config.loop_config
    if force is not None:
        loop_config = replace(loop_config, force=force)
    provider = build_provider(config)
    launcher = None
    if config.inner_loop is not None:
        launcher = build_inner_loop_launcher(config)
    loops_root = _resolve_loops_root(config_path)
    runner = OuterLoopRunner(
        provider,
        loop_config,
        loops_root=loops_root,
        inner_loop_launcher=launcher,
    )
    if run_once:
        runner.run_once(limit=limit)
    else:
        runner.run_forever(limit=limit)


if __name__ == "__main__":
    main()


def _resolve_loops_root(config_path: Path) -> Path:
    """Resolve the loops root directory based on the config path."""

    resolved = config_path.resolve()
    if resolved.parent.name == ".loops":
        return resolved.parent
    return resolved.parent / ".loops"
