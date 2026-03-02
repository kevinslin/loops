"""Backward-compatible alias for `loops.state.approval_config`."""

import sys as _sys

from loops.state import approval_config as _module

_sys.modules[__name__] = _module
