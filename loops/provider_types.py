"""Backward-compatible alias for `loops.state.provider_types`."""

import sys as _sys

from loops.state import provider_types as _module

_sys.modules[__name__] = _module
