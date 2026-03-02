"""Backward-compatible alias for `loops.state.run_record`."""

import sys as _sys

from loops.state import run_record as _module

_sys.modules[__name__] = _module
