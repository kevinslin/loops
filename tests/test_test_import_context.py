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
