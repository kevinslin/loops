"""Backward-compatible alias for `loops.utils.logging`."""

import sys as _sys

from loops.utils import logging as _module

_sys.modules[__name__] = _module
