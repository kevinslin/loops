"""Backward-compatible alias for `loops.state.inner_loop_runtime_config`."""

import sys as _sys

from loops.state import inner_loop_runtime_config as _module

_sys.modules[__name__] = _module
