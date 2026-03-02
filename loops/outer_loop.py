"""Backward-compatible alias for `loops.core.outer_loop`."""

import sys as _sys

from loops.core import outer_loop as _module

_sys.modules[__name__] = _module
