import pytest

from loops.state.approval_config import DEFAULT_APPROVAL_COMMENT_PATTERN
from loops.task_providers.github_projects_v2 import (
    GithubProjectsV2TaskProvider,
    GithubProjectsV2TaskProviderConfig,
    _extract_status_value,
    _map_item_to_task,
    _parse_filters,
    _run_gh_graphql,
    parse_project_url,
)


def test_provider_config_approval_comment_usernames_normalize_on_provider() -> None:
    provider = GithubProjectsV2TaskProvider(
        GithubProjectsV2TaskProviderConfig(
            url="https://github.com/orgs/acme/projects/1",
            approval_comment_usernames=["Maintainer", "review-bot", "maintainer"],
        )
    )
    assert provider.approval_comment_usernames == ("maintainer", "review-bot")


def test_provider_config_approval_comment_pattern_falls_back_when_empty() -> None:
    provider = GithubProjectsV2TaskProvider(
        GithubProjectsV2TaskProviderConfig(
            url="https://github.com/orgs/acme/projects/1",
            approval_comment_pattern="",
        )
    )
    assert provider.approval_comment_pattern == DEFAULT_APPROVAL_COMMENT_PATTERN


def test_provider_config_allowlist_normalizes_on_provider() -> None:
    provider = GithubProjectsV2TaskProvider(
        GithubProjectsV2TaskProviderConfig(
            url="https://github.com/orgs/acme/projects/1",
            allowlist=["Maintainer", "review-bot", "maintainer"],
        )
    )
    assert provider.review_actor_allowlist == ("maintainer", "review-bot")


def test_provider_config_allowlist_empty_denies_all_review_actors() -> None:
    provider = GithubProjectsV2TaskProvider(
        GithubProjectsV2TaskProviderConfig(
            url="https://github.com/orgs/acme/projects/1",
            allowlist=[],
        )
    )
    assert provider.review_actor_allowlist == ()


def test_provider_config_allowlist_wildcard_allows_all_review_actors() -> None:
    provider = GithubProjectsV2TaskProvider(
        GithubProjectsV2TaskProviderConfig(
            url="https://github.com/orgs/acme/projects/1",
            allowlist=["*"],
        )
    )
    assert provider.review_actor_allowlist == ("*",)


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


def test_parse_project_url_user_with_views_suffix() -> None:
    locator = parse_project_url("https://github.com/users/octo/projects/3/views/1")
    assert locator.owner_type == "user"
    assert locator.login == "octo"
    assert locator.number == 3


def test_parse_project_url_invalid() -> None:
    with pytest.raises(ValueError):
        parse_project_url("https://github.com/acme/projects")


def test_parse_project_url_rejects_non_github_host() -> None:
    with pytest.raises(ValueError):
        parse_project_url("https://example.com/orgs/acme/projects/42")


def test_parse_project_url_non_positive() -> None:
    with pytest.raises(ValueError):
        parse_project_url("https://github.com/orgs/acme/projects/0")


def test_parse_filters_normalizes_values() -> None:
    parsed = _parse_filters(
        [
            " repository = Acme/Repo ",
            "tag=Backend",
            "tag=priority/high",
            "tag=backend",
        ]
    )
    assert parsed.repositories == ("acme/repo",)
    assert parsed.tags == ("backend", "priority/high")


def test_parse_filters_rejects_invalid_expression() -> None:
    with pytest.raises(ValueError, match="key=value"):
        _parse_filters(["repository"])


def test_parse_filters_rejects_unsupported_key() -> None:
    with pytest.raises(ValueError, match="Unsupported provider filter key"):
        _parse_filters(["repo=acme/repo"])


def test_parse_filters_rejects_empty_value() -> None:
    with pytest.raises(ValueError, match="value must be non-empty"):
        _parse_filters(["tag=  "])


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

    from loops.task_providers import github_projects_v2

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

    from loops.task_providers import github_projects_v2

    monkeypatch.setattr(github_projects_v2, "_run_gh_graphql", fake_run)

    provider = GithubProjectsV2TaskProvider(
        GithubProjectsV2TaskProviderConfig(
            url="https://github.com/orgs/acme/projects/1",
            github_token="token",
        )
    )
    tasks = provider.poll()
    assert tasks == []


def test_poll_filters_by_repository(monkeypatch) -> None:
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
                                    "title": "Repo A task",
                                    "url": "https://github.com/acme/repo-a/issues/1",
                                    "createdAt": "2026-02-03T00:00:00Z",
                                    "updatedAt": "2026-02-03T01:00:00Z",
                                    "repository": {"nameWithOwner": "acme/repo-a"},
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
                                    "title": "Repo B task",
                                    "url": "https://github.com/acme/repo-b/issues/2",
                                    "createdAt": "2026-02-03T00:00:00Z",
                                    "updatedAt": "2026-02-03T01:00:00Z",
                                    "repository": {"nameWithOwner": "acme/repo-b"},
                                },
                            },
                        ],
                    }
                }
            }
        }
    }

    def fake_run(*, query, variables, github_token, gh_bin):
        return response

    from loops.task_providers import github_projects_v2

    monkeypatch.setattr(github_projects_v2, "_run_gh_graphql", fake_run)

    provider = GithubProjectsV2TaskProvider(
        GithubProjectsV2TaskProviderConfig(
            url="https://github.com/orgs/acme/projects/1",
            github_token="token",
            filters=["repository=acme/repo-a"],
        )
    )
    tasks = provider.poll()
    assert [task.title for task in tasks] == ["Repo A task"]


def test_poll_filters_by_tag(monkeypatch) -> None:
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
                                    "title": "Backend task",
                                    "url": "https://github.com/acme/repo/issues/1",
                                    "createdAt": "2026-02-03T00:00:00Z",
                                    "updatedAt": "2026-02-03T01:00:00Z",
                                    "repository": {"nameWithOwner": "acme/repo"},
                                    "labels": {"nodes": [{"name": "backend"}]},
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
                                    "title": "Frontend task",
                                    "url": "https://github.com/acme/repo/issues/2",
                                    "createdAt": "2026-02-03T00:00:00Z",
                                    "updatedAt": "2026-02-03T01:00:00Z",
                                    "repository": {"nameWithOwner": "acme/repo"},
                                    "labels": {"nodes": [{"name": "frontend"}]},
                                },
                            },
                        ],
                    }
                }
            }
        }
    }

    def fake_run(*, query, variables, github_token, gh_bin):
        return response

    from loops.task_providers import github_projects_v2

    monkeypatch.setattr(github_projects_v2, "_run_gh_graphql", fake_run)

    provider = GithubProjectsV2TaskProvider(
        GithubProjectsV2TaskProviderConfig(
            url="https://github.com/orgs/acme/projects/1",
            github_token="token",
            filters=["tag=backend"],
        )
    )
    tasks = provider.poll()
    assert [task.title for task in tasks] == ["Backend task"]


def test_poll_filters_require_all_tags(monkeypatch) -> None:
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
                                    "title": "Full match",
                                    "url": "https://github.com/acme/repo/issues/1",
                                    "createdAt": "2026-02-03T00:00:00Z",
                                    "updatedAt": "2026-02-03T01:00:00Z",
                                    "repository": {"nameWithOwner": "acme/repo"},
                                    "labels": {
                                        "nodes": [
                                            {"name": "backend"},
                                            {"name": "priority/high"},
                                        ]
                                    },
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
                                    "title": "Partial match",
                                    "url": "https://github.com/acme/repo/issues/2",
                                    "createdAt": "2026-02-03T00:00:00Z",
                                    "updatedAt": "2026-02-03T01:00:00Z",
                                    "repository": {"nameWithOwner": "acme/repo"},
                                    "labels": {"nodes": [{"name": "backend"}]},
                                },
                            },
                        ],
                    }
                }
            }
        }
    }

    def fake_run(*, query, variables, github_token, gh_bin):
        return response

    from loops.task_providers import github_projects_v2

    monkeypatch.setattr(github_projects_v2, "_run_gh_graphql", fake_run)

    provider = GithubProjectsV2TaskProvider(
        GithubProjectsV2TaskProviderConfig(
            url="https://github.com/orgs/acme/projects/1",
            github_token="token",
            filters=["tag=backend", "tag=priority/high"],
        )
    )
    tasks = provider.poll()
    assert [task.title for task in tasks] == ["Full match"]


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

    from loops.task_providers import github_projects_v2

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


def test_poll_filtered_limit_counts_only_matched_items(monkeypatch) -> None:
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
                                    "title": "Unmatched",
                                    "url": "https://github.com/acme/repo/issues/1",
                                    "createdAt": "2026-02-03T00:00:00Z",
                                    "updatedAt": "2026-02-03T01:00:00Z",
                                    "repository": {"nameWithOwner": "acme/repo"},
                                    "labels": {"nodes": [{"name": "frontend"}]},
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
                                    "title": "Matched",
                                    "url": "https://github.com/acme/repo/issues/2",
                                    "createdAt": "2026-02-03T00:00:00Z",
                                    "updatedAt": "2026-02-03T01:00:00Z",
                                    "repository": {"nameWithOwner": "acme/repo"},
                                    "labels": {"nodes": [{"name": "backend"}]},
                                },
                            }
                        ],
                    }
                }
            }
        }
    }
    calls: list[tuple[str | None, int | None]] = []

    def fake_run(*, query, variables, github_token, gh_bin):
        calls.append((variables.get("after"), variables.get("first")))
        if variables.get("after") is None:
            return first_page
        if variables.get("after") == "cursor-1":
            return second_page
        raise AssertionError("Unexpected pagination cursor")

    from loops.task_providers import github_projects_v2

    monkeypatch.setattr(github_projects_v2, "_run_gh_graphql", fake_run)

    provider = GithubProjectsV2TaskProvider(
        GithubProjectsV2TaskProviderConfig(
            url="https://github.com/orgs/acme/projects/1",
            github_token="token",
            filters=["tag=backend"],
        )
    )
    tasks = provider.poll(limit=1)
    assert [task.title for task in tasks] == ["Matched"]
    assert calls == [(None, 50), ("cursor-1", 50)]


def test_poll_orders_oldest_first_before_limit(monkeypatch) -> None:
    first_page = {
        "data": {
            "organization": {
                "projectV2": {
                    "items": {
                        "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
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
                                    "title": "Newer",
                                    "url": "https://github.com/acme/repo/issues/2",
                                    "createdAt": "2026-02-05T00:00:00Z",
                                    "updatedAt": "2026-02-05T01:00:00Z",
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
                                "id": "PVTI_1",
                                "fieldValueByName": {
                                    "__typename": "ProjectV2ItemFieldSingleSelectValue",
                                    "name": "Ready",
                                },
                                "content": {
                                    "__typename": "Issue",
                                    "id": "ISSUE_1",
                                    "title": "Older",
                                    "url": "https://github.com/acme/repo/issues/1",
                                    "createdAt": "2026-02-01T00:00:00Z",
                                    "updatedAt": "2026-02-01T01:00:00Z",
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

    from loops.task_providers import github_projects_v2

    monkeypatch.setattr(github_projects_v2, "_run_gh_graphql", fake_run)

    provider = GithubProjectsV2TaskProvider(
        GithubProjectsV2TaskProviderConfig(
            url="https://github.com/orgs/acme/projects/1",
            github_token="token",
        )
    )
    tasks = provider.poll(limit=1)
    assert [task.title for task in tasks] == ["Older"]
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

    from loops.task_providers import github_projects_v2

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
        "loops.task_providers.github_projects_v2.subprocess.run",
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


def test_update_status_is_idempotent_when_already_target_status(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_run(*, query, variables, github_token, gh_bin):
        calls.append((query, dict(variables)))
        if "updateProjectV2ItemFieldValue" in query:
            raise AssertionError("status mutation should not run when already at target status")
        if "fields(first:" in query:
            return {
                "data": {
                    "organization": {
                        "projectV2": {
                            "id": "PROJ_1",
                            "fields": {
                                "nodes": [
                                    {
                                        "__typename": "ProjectV2SingleSelectField",
                                        "id": "FIELD_STATUS",
                                        "name": "Status",
                                        "options": [
                                            {"id": "OPT_TODO", "name": "Todo"},
                                            {"id": "OPT_PROGRESS", "name": "In Progress"},
                                            {"id": "OPT_DONE", "name": "Done"},
                                        ],
                                    }
                                ]
                            },
                        }
                    }
                }
            }
        return {
            "data": {
                "organization": {
                    "projectV2": {
                        "items": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [
                                {
                                    "id": "ITEM_1",
                                    "fieldValueByName": {
                                        "__typename": "ProjectV2ItemFieldSingleSelectValue",
                                        "name": "In Progress",
                                    },
                                    "content": {
                                        "__typename": "Issue",
                                        "id": "ISSUE_1",
                                        "title": "task",
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

    monkeypatch.setattr("loops.task_providers.github_projects_v2._run_gh_graphql", fake_run)
    provider = GithubProjectsV2TaskProvider(
        GithubProjectsV2TaskProviderConfig(
            url="https://github.com/orgs/acme/projects/1",
            github_token="token",
        )
    )

    provider.update_status("ISSUE_1", "IN_PROGRESS")

    assert all("updateProjectV2ItemFieldValue" not in query for query, _ in calls)


def test_update_status_mutates_when_target_status_differs(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_run(*, query, variables, github_token, gh_bin):
        calls.append((query, dict(variables)))
        if "fields(first:" in query:
            return {
                "data": {
                    "organization": {
                        "projectV2": {
                            "id": "PROJ_1",
                            "fields": {
                                "nodes": [
                                    {
                                        "__typename": "ProjectV2SingleSelectField",
                                        "id": "FIELD_STATUS",
                                        "name": "Status",
                                        "options": [
                                            {"id": "OPT_TODO", "name": "Todo"},
                                            {"id": "OPT_PROGRESS", "name": "In Progress"},
                                            {"id": "OPT_DONE", "name": "Done"},
                                        ],
                                    }
                                ]
                            },
                        }
                    }
                }
            }
        if "updateProjectV2ItemFieldValue" in query:
            return {"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "ITEM_1"}}}}
        return {
            "data": {
                "organization": {
                    "projectV2": {
                        "items": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [
                                {
                                    "id": "ITEM_1",
                                    "fieldValueByName": {
                                        "__typename": "ProjectV2ItemFieldSingleSelectValue",
                                        "name": "Todo",
                                    },
                                    "content": {
                                        "__typename": "Issue",
                                        "id": "ISSUE_1",
                                        "title": "task",
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

    monkeypatch.setattr("loops.task_providers.github_projects_v2._run_gh_graphql", fake_run)
    provider = GithubProjectsV2TaskProvider(
        GithubProjectsV2TaskProviderConfig(
            url="https://github.com/orgs/acme/projects/1",
            github_token="token",
        )
    )

    provider.update_status("ISSUE_1", "DONE")

    mutation_calls = [
        variables
        for query, variables in calls
        if "updateProjectV2ItemFieldValue" in query
    ]
    assert mutation_calls == [
        {
            "projectId": "PROJ_1",
            "itemId": "ITEM_1",
            "fieldId": "FIELD_STATUS",
            "optionId": "OPT_DONE",
        }
    ]


def test_update_status_paginates_status_field_lookup(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_run(*, query, variables, github_token, gh_bin):
        calls.append((query, dict(variables)))
        if "updateProjectV2ItemFieldValue" in query:
            return {"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "ITEM_1"}}}}
        if "fields(first:" in query:
            fields_after = variables.get("fieldsAfter")
            if fields_after is None:
                return {
                    "data": {
                        "organization": {
                            "projectV2": {
                                "id": "PROJ_1",
                                "fields": {
                                    "pageInfo": {"hasNextPage": True, "endCursor": "CURSOR_1"},
                                    "nodes": [
                                        {
                                            "__typename": "ProjectV2SingleSelectField",
                                            "id": "FIELD_PRIORITY",
                                            "name": "Priority",
                                            "options": [
                                                {"id": "OPT_HIGH", "name": "High"},
                                            ],
                                        }
                                    ],
                                },
                            }
                        }
                    }
                }
            if fields_after == "CURSOR_1":
                return {
                    "data": {
                        "organization": {
                            "projectV2": {
                                "id": "PROJ_1",
                                "fields": {
                                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                                    "nodes": [
                                        {
                                            "__typename": "ProjectV2SingleSelectField",
                                            "id": "FIELD_STATUS",
                                            "name": "Status",
                                            "options": [
                                                {"id": "OPT_TODO", "name": "Todo"},
                                                {"id": "OPT_PROGRESS", "name": "In Progress"},
                                                {"id": "OPT_DONE", "name": "Done"},
                                            ],
                                        }
                                    ],
                                },
                            }
                        }
                    }
                }
            raise AssertionError(f"unexpected fieldsAfter value: {fields_after}")
        return {
            "data": {
                "organization": {
                    "projectV2": {
                        "items": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [
                                {
                                    "id": "ITEM_1",
                                    "fieldValueByName": {
                                        "__typename": "ProjectV2ItemFieldSingleSelectValue",
                                        "name": "Todo",
                                    },
                                    "content": {
                                        "__typename": "Issue",
                                        "id": "ISSUE_1",
                                        "title": "task",
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

    monkeypatch.setattr("loops.task_providers.github_projects_v2._run_gh_graphql", fake_run)
    provider = GithubProjectsV2TaskProvider(
        GithubProjectsV2TaskProviderConfig(
            url="https://github.com/orgs/acme/projects/1",
            github_token="token",
        )
    )

    provider.update_status("ISSUE_1", "IN_PROGRESS")

    field_calls = [variables for query, variables in calls if "fields(first:" in query]
    assert field_calls == [
        {
            "login": "acme",
            "number": 1,
            "fieldsFirst": 100,
            "fieldsAfter": None,
        },
        {
            "login": "acme",
            "number": 1,
            "fieldsFirst": 100,
            "fieldsAfter": "CURSOR_1",
        },
    ]
    mutation_calls = [
        variables
        for query, variables in calls
        if "updateProjectV2ItemFieldValue" in query
    ]
    assert mutation_calls == [
        {
            "projectId": "PROJ_1",
            "itemId": "ITEM_1",
            "fieldId": "FIELD_STATUS",
            "optionId": "OPT_PROGRESS",
        }
    ]


def test_update_status_raises_when_status_mapping_missing(monkeypatch) -> None:
    def fake_run(*, query, variables, github_token, gh_bin):
        if "fields(first:" in query:
            return {
                "data": {
                    "organization": {
                        "projectV2": {
                            "id": "PROJ_1",
                            "fields": {
                                "nodes": [
                                    {
                                        "__typename": "ProjectV2SingleSelectField",
                                        "id": "FIELD_STATUS",
                                        "name": "Status",
                                        "options": [
                                            {"id": "OPT_READY", "name": "Ready"},
                                            {"id": "OPT_BLOCKED", "name": "Blocked"},
                                        ],
                                    }
                                ]
                            },
                        }
                    }
                }
            }
        return {
            "data": {
                "organization": {
                    "projectV2": {
                        "items": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [
                                {
                                    "id": "ITEM_1",
                                    "fieldValueByName": {
                                        "__typename": "ProjectV2ItemFieldSingleSelectValue",
                                        "name": "Ready",
                                    },
                                    "content": {
                                        "__typename": "Issue",
                                        "id": "ISSUE_1",
                                        "title": "task",
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

    monkeypatch.setattr("loops.task_providers.github_projects_v2._run_gh_graphql", fake_run)
    provider = GithubProjectsV2TaskProvider(
        GithubProjectsV2TaskProviderConfig(
            url="https://github.com/orgs/acme/projects/1",
            github_token="token",
        )
    )

    with pytest.raises(RuntimeError, match="could not map internal status"):
        provider.update_status("ISSUE_1", "IN_PROGRESS")
