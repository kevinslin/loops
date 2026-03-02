"""Backward-compatible alias for `loops.core.cli`."""

import sys as _sys

from loops.core import cli as _module

_sys.modules[__name__] = _module
