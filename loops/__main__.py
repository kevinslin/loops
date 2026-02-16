"""Module entrypoint for `python -m loops`."""

import sys

from loops.cli import main


_CLI_SUBCOMMANDS = {"init", "run", "inner-loop", "signal"}


def _normalize_argv(argv: list[str]) -> list[str]:
    """Normalize argv so legacy outer-loop invocations continue to work."""

    if len(argv) <= 1:
        return [argv[0], "run"]
    first = argv[1]
    if first in _CLI_SUBCOMMANDS or first in {"-h", "--help"}:
        return argv
    return [argv[0], "run", *argv[1:]]


def entrypoint(argv: list[str] | None = None) -> None:
    """Run the Loops CLI with legacy argv normalization."""

    raw_argv = sys.argv if argv is None else argv
    sys.argv = _normalize_argv(raw_argv)
    main(prog_name="loops")


if __name__ == "__main__":
    entrypoint()
