"""Backward-compatible alias for `loops.core.inner_loop`."""

import sys as _sys

from loops.core import inner_loop as _module

_sys.modules[__name__] = _module

if __name__ == "__main__":
    _module.main()
