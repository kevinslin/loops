"""Backward-compatible alias for `loops.commands.clean`."""

import sys as _sys

from loops.commands import clean as _module

_sys.modules[__name__] = _module
