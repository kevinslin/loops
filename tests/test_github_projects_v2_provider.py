import pytest

from loops.providers.github_projects_v2 import (
    GithubProjectsV2TaskProvider,
    GithubProjectsV2TaskProviderConfig,
    _extract_status_value,
    _map_item_to_task,
    _run_gh_graphql,
    parse_project_url,
)


def test_parse_project_url_org() -> None:
    locator = parse_project_url("https://github.com/orgs/acme/projects/42")
    assert locator.owner_type == "organization"
    assert locator.login == "acme"
    assert locator.number == 42


def test_parse_project_url_user() -> None:
    locator = parse_project_url("https://github.com/users/octo/projects/3")
    assert locator.owner_type == "user"
    assert locator.login == "octo"
    assert locator.number == 3


def test_parse_project_url_invalid() -> None:
    with pytest.raises(ValueError):
        parse_project_url("https://github.com/acme/projects")


def test_parse_project_url_non_positive() -> None:
    with pytest.raises(ValueError):
        parse_project_url("https://github.com/orgs/acme/projects/0")


def test_extract_status_value() -> None:
    assert (
        _extract_status_value(
            {
                "__typename": "ProjectV2ItemFieldSingleSelectValue",
                "name": "Ready",
            }
        )
        == "Ready"
    )
    assert (
        _extract_status_value(
            {
                "__typename": "ProjectV2ItemFieldTextValue",
                "text": "Backlog",
            }
        )
        == "Backlog"
    )
    assert _extract_status_value(None) == ""


def test_map_item_to_task_issue() -> None:
    item = {
        "id": "PVTI_1",
        "fieldValueByName": {
            "__typename": "ProjectV2ItemFieldSingleSelectValue",
            "name": "Ready",
        },
        "content": {
            "__typename": "Issue",
            "id": "ISSUE_1",
            "title": "Ship it",
            "url": "https://github.com/acme/repo/issues/1",
            "createdAt": "2026-02-03T00:00:00Z",
            "updatedAt": "2026-02-03T01:00:00Z",
            "repository": {"nameWithOwner": "acme/repo"},
        },
    }
    task = _map_item_to_task(item)
    assert task is not None
    assert task.title == "Ship it"
    assert task.status == "Ready"
    assert task.repo == "acme/repo"


def test_poll_maps_tasks(monkeypatch) -> None:
    response = {
        "data": {
            "organization": {
                "projectV2": {
                    "items": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            {
                                "id": "PVTI_1",
                                "fieldValueByName": {
                                    "__typename": "ProjectV2ItemFieldSingleSelectValue",
                                    "name": "Ready",
                                },
                                "content": {
                                    "__typename": "Issue",
                                    "id": "ISSUE_1",
                                    "title": "Ship it",
                                    "url": "https://github.com/acme/repo/issues/1",
                                    "createdAt": "2026-02-03T00:00:00Z",
                                    "updatedAt": "2026-02-03T01:00:00Z",
                                    "repository": {"nameWithOwner": "acme/repo"},
                                },
                            },
                            {
                                "id": "PVTI_2",
                                "fieldValueByName": {
                                    "__typename": "ProjectV2ItemFieldSingleSelectValue",
                                    "name": "Ready",
                                },
                                "content": {
                                    "__typename": "Issue",
                                    "id": "ISSUE_2",
                                    "title": "Next",
                                    "url": "https://github.com/acme/repo/issues/2",
                                    "createdAt": "2026-02-03T00:00:00Z",
                                    "updatedAt": "2026-02-03T01:00:00Z",
                                    "repository": {"nameWithOwner": "acme/repo"},
                                },
                            },
                        ],
                    }
                }
            }
        }
    }

    def fake_run(*, query, variables, github_token, gh_bin):
        assert variables["login"] == "acme"
        assert variables["number"] == 1
        assert variables["statusField"] == "Status"
        assert github_token == "token"
        return response

    from loops.providers import github_projects_v2

    monkeypatch.setattr(github_projects_v2, "_run_gh_graphql", fake_run)

    provider = GithubProjectsV2TaskProvider(
        GithubProjectsV2TaskProviderConfig(
            url="https://github.com/orgs/acme/projects/1",
            github_token="token",
        )
    )
    tasks = provider.poll(limit=1)
    assert len(tasks) == 1
    assert tasks[0].title == "Ship it"


def test_poll_handles_empty_results(monkeypatch) -> None:
    response = {
        "data": {
            "organization": {
                "projectV2": {
                    "items": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [],
                    }
                }
            }
        }
    }

    def fake_run(*, query, variables, github_token, gh_bin):
        return response

    from loops.providers import github_projects_v2

    monkeypatch.setattr(github_projects_v2, "_run_gh_graphql", fake_run)

    provider = GithubProjectsV2TaskProvider(
        GithubProjectsV2TaskProviderConfig(
            url="https://github.com/orgs/acme/projects/1",
            github_token="token",
        )
    )
    tasks = provider.poll()
    assert tasks == []


def test_poll_paginates(monkeypatch) -> None:
    first_page = {
        "data": {
            "organization": {
                "projectV2": {
                    "items": {
                        "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                        "nodes": [
                            {
                                "id": "PVTI_1",
                                "fieldValueByName": {
                                    "__typename": "ProjectV2ItemFieldSingleSelectValue",
                                    "name": "Ready",
                                },
                                "content": {
                                    "__typename": "Issue",
                                    "id": "ISSUE_1",
                                    "title": "First",
                                    "url": "https://github.com/acme/repo/issues/1",
                                    "createdAt": "2026-02-03T00:00:00Z",
                                    "updatedAt": "2026-02-03T01:00:00Z",
                                    "repository": {"nameWithOwner": "acme/repo"},
                                },
                            }
                        ],
                    }
                }
            }
        }
    }
    second_page = {
        "data": {
            "organization": {
                "projectV2": {
                    "items": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            {
                                "id": "PVTI_2",
                                "fieldValueByName": {
                                    "__typename": "ProjectV2ItemFieldSingleSelectValue",
                                    "name": "Ready",
                                },
                                "content": {
                                    "__typename": "Issue",
                                    "id": "ISSUE_2",
                                    "title": "Second",
                                    "url": "https://github.com/acme/repo/issues/2",
                                    "createdAt": "2026-02-03T00:00:00Z",
                                    "updatedAt": "2026-02-03T01:00:00Z",
                                    "repository": {"nameWithOwner": "acme/repo"},
                                },
                            }
                        ],
                    }
                }
            }
        }
    }
    calls: list[str | None] = []

    def fake_run(*, query, variables, github_token, gh_bin):
        calls.append(variables.get("after"))
        if variables.get("after") is None:
            return first_page
        if variables.get("after") == "cursor-1":
            return second_page
        raise AssertionError("Unexpected pagination cursor")

    from loops.providers import github_projects_v2

    monkeypatch.setattr(github_projects_v2, "_run_gh_graphql", fake_run)

    provider = GithubProjectsV2TaskProvider(
        GithubProjectsV2TaskProviderConfig(
            url="https://github.com/orgs/acme/projects/1",
            github_token="token",
        )
    )
    tasks = provider.poll()
    assert [task.title for task in tasks] == ["First", "Second"]
    assert calls == [None, "cursor-1"]


def test_poll_missing_cursor_raises(monkeypatch) -> None:
    response = {
        "data": {
            "organization": {
                "projectV2": {
                    "items": {
                        "pageInfo": {"hasNextPage": True, "endCursor": None},
                        "nodes": [],
                    }
                }
            }
        }
    }

    def fake_run(*, query, variables, github_token, gh_bin):
        return response

    from loops.providers import github_projects_v2

    monkeypatch.setattr(github_projects_v2, "_run_gh_graphql", fake_run)

    provider = GithubProjectsV2TaskProvider(
        GithubProjectsV2TaskProviderConfig(
            url="https://github.com/orgs/acme/projects/1",
            github_token="token",
        )
    )
    with pytest.raises(RuntimeError):
        provider.poll()


def test_poll_requires_token(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    provider = GithubProjectsV2TaskProvider(
        GithubProjectsV2TaskProviderConfig(
            url="https://github.com/orgs/acme/projects/1"
        )
    )

    with pytest.raises(RuntimeError, match="GITHUB_TOKEN"):
        provider.poll()


def test_run_gh_graphql_uses_typed_fields_for_numeric_variables(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_subprocess_run(
        args,
        *,
        check,
        capture_output,
        text,
        timeout,
        env,
    ):
        captured["args"] = args
        captured["check"] = check
        captured["capture_output"] = capture_output
        captured["text"] = text
        captured["timeout"] = timeout
        captured["env"] = env

        class Result:
            stdout = '{"data": {}}'

        return Result()

    monkeypatch.setattr(
        "loops.providers.github_projects_v2.subprocess.run",
        fake_subprocess_run,
    )

    payload = _run_gh_graphql(
        query="query { viewer { login } }",
        variables={
            "login": "acme",
            "number": 1,
            "first": 50,
            "statusField": "Status",
        },
        github_token="token-123",
        gh_bin="gh",
    )

    assert payload == {"data": {}}

    args = captured["args"]
    assert isinstance(args, list)
    assert args[:3] == ["gh", "api", "graphql"]
    assert "-f" in args
    assert "-F" in args

    # Strings should use -f, numeric variables must use typed -F.
    query_idx = args.index("query=query { viewer { login } }")
    login_idx = args.index("login=acme")
    number_idx = args.index("number=1")
    first_idx = args.index("first=50")
    status_idx = args.index("statusField=Status")
    assert args[query_idx - 1] == "-f"
    assert args[login_idx - 1] == "-f"
    assert args[number_idx - 1] == "-F"
    assert args[first_idx - 1] == "-F"
    assert args[status_idx - 1] == "-f"

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["GITHUB_TOKEN"] == "token-123"
    assert env["GH_TOKEN"] == "token-123"
