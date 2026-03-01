"""Pytest session bootstrap helpers."""

from __future__ import annotations

import os
from pathlib import Path
import sys


def _canonicalize_path(path: str) -> str:
    """Return a canonical path string for dedupe comparisons."""
    candidate = path or "."
    return os.path.realpath(candidate)


def ensure_repo_root_first() -> str:
    """Ensure the active repository root is first in sys.path."""
    repo_root = str(Path(__file__).resolve().parents[1])
    repo_root_canonical = _canonicalize_path(repo_root)
    filtered_path = [
        entry
        for entry in sys.path
        if _canonicalize_path(entry) != repo_root_canonical
    ]
    sys.path[:] = [repo_root, *filtered_path]
    return repo_root


REPO_ROOT = ensure_repo_root_first()
