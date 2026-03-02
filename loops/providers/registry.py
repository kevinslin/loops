"""Backward-compatible alias for `loops.task_providers.registry`."""

import sys as _sys

from loops.task_providers import registry as _module

_sys.modules[__name__] = _module
