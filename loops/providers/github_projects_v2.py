from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

from loops.run_record import Task

GITHUB_PROJECTS_V2_PROVIDER_ID = "github_projects_v2"
OwnerType = Literal["organization", "user"]


@dataclass(frozen=True)
class GithubProjectsV2TaskProviderConfig:
    url: str
    status_field: str = "Status"
    page_size: int = 50


@dataclass(frozen=True)
class ProjectLocator:
    owner_type: OwnerType
    login: str
    number: int


def parse_project_url(url: str) -> ProjectLocator:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 4:
        raise ValueError(
            "Project URL must look like https://github.com/orgs/<org>/projects/<number>"
        )
    scope, login, projects_keyword, number = parts[0], parts[1], parts[2], parts[3]
    if scope not in {"orgs", "users"} or projects_keyword != "projects":
        raise ValueError(
            "Project URL must look like https://github.com/orgs/<org>/projects/<number>"
        )
    try:
        project_number = int(number)
    except ValueError as exc:
        raise ValueError("Project URL must end with a numeric project id") from exc
    owner_type: OwnerType = "organization" if scope == "orgs" else "user"
    return ProjectLocator(owner_type=owner_type, login=login, number=project_number)


_ORG_QUERY = """
query($login: String!, $number: Int!, $after: String, $first: Int!, $statusField: String!) {
  organization(login: $login) {
    projectV2(number: $number) {
      items(first: $first, after: $after) {
        pageInfo {
          endCursor
          hasNextPage
        }
        nodes {
          id
          fieldValueByName(name: $statusField) {
            __typename
            ... on ProjectV2ItemFieldSingleSelectValue {
              name
            }
            ... on ProjectV2ItemFieldTextValue {
              text
            }
          }
          content {
            __typename
            ... on Issue {
              id
              title
              url
              createdAt
              updatedAt
              repository {
                nameWithOwner
              }
            }
            ... on PullRequest {
              id
              title
              url
              createdAt
              updatedAt
              repository {
                nameWithOwner
              }
            }
          }
        }
      }
    }
  }
}
"""

_USER_QUERY = """
query($login: String!, $number: Int!, $after: String, $first: Int!, $statusField: String!) {
  user(login: $login) {
    projectV2(number: $number) {
      items(first: $first, after: $after) {
        pageInfo {
          endCursor
          hasNextPage
        }
        nodes {
          id
          fieldValueByName(name: $statusField) {
            __typename
            ... on ProjectV2ItemFieldSingleSelectValue {
              name
            }
            ... on ProjectV2ItemFieldTextValue {
              text
            }
          }
          content {
            __typename
            ... on Issue {
              id
              title
              url
              createdAt
              updatedAt
              repository {
                nameWithOwner
              }
            }
            ... on PullRequest {
              id
              title
              url
              createdAt
              updatedAt
              repository {
                nameWithOwner
              }
            }
          }
        }
      }
    }
  }
}
"""


class GithubProjectsV2TaskProvider:
    def __init__(
        self,
        config: GithubProjectsV2TaskProviderConfig,
        gh_bin: str = "gh",
    ) -> None:
        self.config = config
        self.gh_bin = gh_bin

    def poll(self, limit: int | None = None) -> list[Task]:
        if limit is not None and limit <= 0:
            return []

        locator = parse_project_url(self.config.url)
        tasks: list[Task] = []
        after: str | None = None
        while True:
            remaining = None if limit is None else max(limit - len(tasks), 0)
            if remaining == 0:
                return tasks
            page_size = self._page_size(remaining)
            response = _run_gh_graphql(
                query=_select_query(locator.owner_type),
                variables={
                    "login": locator.login,
                    "number": locator.number,
                    "after": after,
                    "first": page_size,
                    "statusField": self.config.status_field,
                },
                gh_bin=self.gh_bin,
            )
            items, page_info = _extract_items(response, locator.owner_type)
            for item in items:
                task = _map_item_to_task(item)
                if task is None:
                    continue
                tasks.append(task)
                if limit is not None and len(tasks) >= limit:
                    return tasks
            if not page_info.get("hasNextPage"):
                return tasks
            after = page_info.get("endCursor")

    def _page_size(self, remaining: int | None) -> int:
        if remaining is None:
            return self.config.page_size
        return min(self.config.page_size, remaining)


def _select_query(owner_type: OwnerType) -> str:
    if owner_type == "organization":
        return _normalize_query(_ORG_QUERY)
    return _normalize_query(_USER_QUERY)


def _normalize_query(query: str) -> str:
    return " ".join(query.split())


def _run_gh_graphql(
    *,
    query: str,
    variables: dict[str, Any],
    gh_bin: str,
) -> dict[str, Any]:
    args = [gh_bin, "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        if value is None:
            continue
        args.extend(["-f", f"{key}={value}"])
    try:
        result = subprocess.run(
            args,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip()
        raise RuntimeError(f"gh api graphql failed: {stderr}") from exc
    payload = json.loads(result.stdout)
    if payload.get("errors"):
        raise RuntimeError(f"GraphQL returned errors: {payload['errors']}")
    return payload


def _extract_items(
    payload: dict[str, Any],
    owner_type: OwnerType,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    data = payload.get("data") or {}
    owner_key = "organization" if owner_type == "organization" else "user"
    owner = data.get(owner_key) or {}
    project = owner.get("projectV2") or {}
    items = project.get("items") or {}
    nodes = items.get("nodes") or []
    page_info = items.get("pageInfo") or {}
    return nodes, page_info


def _extract_status_value(field_value: dict[str, Any] | None) -> str:
    if not field_value:
        return ""
    typename = field_value.get("__typename")
    if typename == "ProjectV2ItemFieldSingleSelectValue":
        return str(field_value.get("name") or "")
    if typename == "ProjectV2ItemFieldTextValue":
        return str(field_value.get("text") or "")
    return ""


def _map_item_to_task(item: dict[str, Any]) -> Task | None:
    content = item.get("content")
    if not content:
        return None
    typename = content.get("__typename")
    if typename not in {"Issue", "PullRequest"}:
        return None
    content_id = content.get("id")
    title = content.get("title")
    url = content.get("url")
    created_at = content.get("createdAt")
    updated_at = content.get("updatedAt")
    if not all([content_id, title, url, created_at, updated_at]):
        return None
    repo_info = content.get("repository") or {}
    repo = repo_info.get("nameWithOwner")
    status = _extract_status_value(item.get("fieldValueByName"))
    return Task(
        provider_id=GITHUB_PROJECTS_V2_PROVIDER_ID,
        id=str(content_id),
        title=str(title),
        status=status,
        url=str(url),
        created_at=str(created_at),
        updated_at=str(updated_at),
        repo=str(repo) if repo else None,
    )
