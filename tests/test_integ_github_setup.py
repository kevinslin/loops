from __future__ import annotations

import subprocess

import pytest

from tests.integ import github_setup


def _build_issue(number: int, *, node_id: str, title: str) -> github_setup.IssueHandle:
    return github_setup.IssueHandle(
        number=number,
        node_id=node_id,
        url=f"https://github.com/kevinslin/loops-integ/issues/{number}",
        title=title,
    )


def test_create_live_issue_bundle_rolls_back_on_setup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = github_setup.ProjectMetadata(
        project_id="PROJ",
        status_field_id="STATUS",
        ready_option_id="READY",
        non_ready_option_id="BACKLOG",
        completed_option_id="DONE",
    )
    locator = github_setup.ProjectLocator(owner_type="user", login="kevinslin", number=6)
    issue1 = _build_issue(number=101, node_id="ISSUE1", title="task1")
    issue2 = _build_issue(number=102, node_id="ISSUE2", title="task2")

    monkeypatch.setattr(github_setup, "parse_project_url", lambda _url: locator)
    monkeypatch.setattr(
        github_setup,
        "fetch_project_metadata",
        lambda _locator, *, token: project,
    )
    monkeypatch.setattr(github_setup, "build_run_label", lambda: "run-label")
    monkeypatch.setattr(github_setup, "ensure_repo_label", lambda **_kwargs: None)
    issue_iter = iter([issue1, issue2])
    monkeypatch.setattr(github_setup, "create_issue", lambda **_kwargs: next(issue_iter))
    item_iter = iter(["ITEM1", "ITEM2"])
    monkeypatch.setattr(
        github_setup,
        "add_issue_to_project",
        lambda **_kwargs: next(item_iter),
    )

    def _fail_set_status(**_kwargs: str) -> None:
        raise RuntimeError("status update failed")

    monkeypatch.setattr(github_setup, "set_project_item_status", _fail_set_status)
    deleted_items: list[str] = []
    closed_issues: list[int] = []
    monkeypatch.setattr(
        github_setup,
        "delete_project_item",
        lambda *, project_id, item_id, token: deleted_items.append(item_id),
    )
    monkeypatch.setattr(
        github_setup,
        "close_issue",
        lambda *, repo, issue_number, token: closed_issues.append(issue_number),
    )

    with pytest.raises(RuntimeError, match="status update failed"):
        github_setup.create_live_issue_bundle(token="token")

    assert deleted_items == ["ITEM1", "ITEM2"]
    assert closed_issues == [101, 102]


def test_create_live_issue_bundle_reports_cleanup_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = github_setup.ProjectMetadata(
        project_id="PROJ",
        status_field_id="STATUS",
        ready_option_id="READY",
        non_ready_option_id="BACKLOG",
        completed_option_id="DONE",
    )
    locator = github_setup.ProjectLocator(owner_type="user", login="kevinslin", number=6)
    issue1 = _build_issue(number=201, node_id="ISSUE201", title="task1")
    issue2 = _build_issue(number=202, node_id="ISSUE202", title="task2")

    monkeypatch.setattr(github_setup, "parse_project_url", lambda _url: locator)
    monkeypatch.setattr(
        github_setup,
        "fetch_project_metadata",
        lambda _locator, *, token: project,
    )
    monkeypatch.setattr(github_setup, "build_run_label", lambda: "run-label")
    monkeypatch.setattr(github_setup, "ensure_repo_label", lambda **_kwargs: None)
    issue_iter = iter([issue1, issue2])
    monkeypatch.setattr(github_setup, "create_issue", lambda **_kwargs: next(issue_iter))
    monkeypatch.setattr(github_setup, "add_issue_to_project", lambda **_kwargs: "ITEM")

    def _fail_set_status(**_kwargs: str) -> None:
        raise RuntimeError("status update failed")

    def _fail_delete(*, project_id: str, item_id: str, token: str) -> None:
        raise RuntimeError("delete failed")

    def _fail_close(*, repo: str, issue_number: int, token: str) -> None:
        raise RuntimeError("close failed")

    monkeypatch.setattr(github_setup, "set_project_item_status", _fail_set_status)
    monkeypatch.setattr(github_setup, "delete_project_item", _fail_delete)
    monkeypatch.setattr(github_setup, "close_issue", _fail_close)

    with pytest.raises(RuntimeError, match="partial cleanup failed"):
        github_setup.create_live_issue_bundle(token="token")


def test_run_gh_raises_timeout_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOOPS_INTEG_GH_TIMEOUT_SECONDS", "5")

    def _timeout(*_args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs.get("timeout") == 5.0
        raise subprocess.TimeoutExpired(cmd=["gh", "api"], timeout=5.0)

    monkeypatch.setattr(github_setup.subprocess, "run", _timeout)

    with pytest.raises(RuntimeError, match="gh command timed out"):
        github_setup.run_gh(["api", "graphql"], token="token", check=False)


def test_run_gh_rejects_invalid_timeout_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOOPS_INTEG_GH_TIMEOUT_SECONDS", "not-a-number")

    with pytest.raises(ValueError, match="LOOPS_INTEG_GH_TIMEOUT_SECONDS must be a number"):
        github_setup.run_gh(["api", "graphql"], token="token", check=False)


def test_select_completed_option_id_prefers_done() -> None:
    option_id = github_setup.select_completed_option_id(
        option_by_name={"completed": "COMPLETE", "done": "DONE"},
    )

    assert option_id == "DONE"


def test_select_completed_option_id_falls_back_to_completed() -> None:
    option_id = github_setup.select_completed_option_id(
        option_by_name={"completed": "COMPLETE"},
    )

    assert option_id == "COMPLETE"


def test_select_completed_option_id_raises_when_missing() -> None:
    with pytest.raises(RuntimeError, match="completed status option"):
        github_setup.select_completed_option_id(option_by_name={"todo": "TODO"})


def test_create_end2end_issue_bundle_rolls_back_on_setup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = github_setup.ProjectMetadata(
        project_id="PROJ",
        status_field_id="STATUS",
        ready_option_id="READY",
        non_ready_option_id="BACKLOG",
        completed_option_id="DONE",
    )
    locator = github_setup.ProjectLocator(owner_type="user", login="kevinslin", number=6)
    issue = _build_issue(number=301, node_id="ISSUE301", title="task")

    monkeypatch.setattr(github_setup, "parse_project_url", lambda _url: locator)
    monkeypatch.setattr(
        github_setup,
        "fetch_project_metadata",
        lambda _locator, *, token: project,
    )
    monkeypatch.setattr(github_setup, "build_run_label", lambda: "run-label")
    monkeypatch.setattr(github_setup, "ensure_repo_label", lambda **_kwargs: None)
    monkeypatch.setattr(github_setup, "create_issue", lambda **_kwargs: issue)
    monkeypatch.setattr(github_setup, "add_issue_to_project", lambda **_kwargs: "ITEM301")

    def _fail_set_status(**_kwargs: str) -> None:
        raise RuntimeError("status update failed")

    monkeypatch.setattr(github_setup, "set_project_item_status", _fail_set_status)
    deleted_items: list[str] = []
    closed_issues: list[int] = []
    monkeypatch.setattr(
        github_setup,
        "delete_project_item",
        lambda *, project_id, item_id, token: deleted_items.append(item_id),
    )
    monkeypatch.setattr(
        github_setup,
        "close_issue",
        lambda *, repo, issue_number, token: closed_issues.append(issue_number),
    )

    with pytest.raises(RuntimeError, match="status update failed"):
        github_setup.create_end2end_issue_bundle(token="token")

    assert deleted_items == ["ITEM301"]
    assert closed_issues == [301]
