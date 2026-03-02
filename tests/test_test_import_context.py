from __future__ import annotations

from pathlib import Path
import sys

import conftest


def test_ensure_repo_root_first_moves_repo_to_index_zero(monkeypatch) -> None:
    repo_root = str(Path(__file__).resolve().parents[1])
    monkeypatch.setattr(
        sys,
        "path",
        ["/tmp/fake-loop-path", "/usr/lib/python3.14", repo_root],
    )

    resolved_root = conftest.ensure_repo_root_first()

    assert resolved_root == repo_root
    assert sys.path[0] == repo_root
    assert sys.path.count(repo_root) == 1
    assert "/tmp/fake-loop-path" in sys.path


def test_ensure_repo_root_first_dedupes_alias_paths(monkeypatch) -> None:
    repo_root = str(Path(__file__).resolve().parents[1])
    repo_alias = f"{repo_root}/"
    monkeypatch.setattr(
        sys,
        "path",
        [repo_alias, "/tmp/fake-loop-path", repo_root],
    )

    conftest.ensure_repo_root_first()

    canonical_entries = [conftest._canonicalize_path(entry) for entry in sys.path]
    repo_canonical = conftest._canonicalize_path(repo_root)

    assert sys.path[0] == repo_root
    assert canonical_entries.count(repo_canonical) == 1
