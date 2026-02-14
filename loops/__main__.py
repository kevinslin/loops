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


if __name__ == "__main__":
    sys.argv = _normalize_argv(sys.argv)
    main(prog_name="loops")
