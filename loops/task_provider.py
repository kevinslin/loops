"""Backward-compatible alias for `loops.task_providers.base`."""

import sys as _sys

from loops.task_providers import base as _module

_sys.modules[__name__] = _module
