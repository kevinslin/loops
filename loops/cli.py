"""Backward-compatible alias for `loops.core.cli`."""

import sys as _sys

from loops.core import cli as _module

_sys.modules[__name__] = _module

if __name__ == "__main__":
    raise SystemExit("`python -m loops.cli` is unsupported; use `loops` or `python -m loops`.")
