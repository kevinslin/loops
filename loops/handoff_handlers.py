"""Backward-compatible alias for `loops.core.handoff_handlers`."""

import sys as _sys

from loops.core import handoff_handlers as _module

_sys.modules[__name__] = _module
