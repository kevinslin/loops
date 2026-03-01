"""Pytest session bootstrap helpers."""

from __future__ import annotations

from pathlib import Path
import sys


def ensure_repo_root_first() -> str:
    """Ensure the active repository root is first in sys.path."""
    repo_root = str(Path(__file__).resolve().parents[1])
    filtered_path = [entry for entry in sys.path if entry != repo_root]
    sys.path[:] = [repo_root, *filtered_path]
    return repo_root


REPO_ROOT = ensure_repo_root_first()
