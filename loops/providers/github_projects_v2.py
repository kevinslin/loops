from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from loops.provider_types import LoopsProviderConfig, SecretRequirement
from loops.run_record import Task

GITHUB_PROJECTS_V2_PROVIDER_ID = "github_projects_v2"
OwnerType = Literal["organization", "user"]
GH_API_TIMEOUT_SECONDS = 30


class GithubProjectsV2TaskProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    status_field: str = "Status"
    page_size: int = Field(default=50, gt=0)
    github_token: str | None = None
    filters: list[str] = Field(default_factory=list)


GITHUB_PROJECTS_V2_PROVIDER_CONFIG = LoopsProviderConfig(
    id=GITHUB_PROJECTS_V2_PROVIDER_ID,
    name="GitHub Projects V2",
    required_secrets=(
        SecretRequirement(
            name="GITHUB_TOKEN",
            alias=("GH_TOKEN",),
            description=(
                "GitHub API token used to poll project items "
                "(set GITHUB_TOKEN or GH_TOKEN)."
            ),
        ),
    ),
    provider_config_model=GithubProjectsV2TaskProviderConfig,
)


@dataclass(frozen=True)
class ProjectLocator:
    owner_type: OwnerType
    login: str
    number: int


@dataclass(frozen=True)
class ProjectFilters:
    repositories: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        return not self.repositories and not self.tags


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
    if project_number <= 0:
        raise ValueError("Project id must be a positive integer")
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
              labels(first: 100) {
                nodes {
                  name
                }
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
              labels(first: 100) {
                nodes {
                  name
                }
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
              labels(first: 100) {
                nodes {
                  name
                }
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
              labels(first: 100) {
                nodes {
                  name
                }
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
        self.filters = _parse_filters(config.filters)

    def poll(self, limit: int | None = None) -> list[Task]:
        if limit is not None and limit <= 0:
            return []

        github_token = _resolve_github_token(self.config)
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
                github_token=github_token,
                gh_bin=self.gh_bin,
            )
            items, page_info = _extract_items(response, locator.owner_type)
            for item in items:
                task = _map_item_to_task(item)
                if task is None:
                    continue
                if not _matches_filters(task, item, self.filters):
                    continue
                tasks.append(task)
                if limit is not None and len(tasks) >= limit:
                    return tasks
            has_next_page = bool(page_info.get("hasNextPage"))
            if not has_next_page:
                return tasks
            after = page_info.get("endCursor")
            if not after:
                raise RuntimeError("Pagination missing endCursor while hasNextPage=true")

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


def _parse_filters(expressions: list[str]) -> ProjectFilters:
    repositories: list[str] = []
    tags: list[str] = []
    for index, raw_expression in enumerate(expressions):
        expression = raw_expression.strip()
        if not expression:
            raise ValueError(f"filters[{index}] must be non-empty")
        if "=" not in expression:
            raise ValueError(
                f"filters[{index}] must use key=value syntax: {raw_expression!r}"
            )
        raw_key, raw_value = expression.split("=", 1)
        key = raw_key.strip().casefold()
        value = raw_value.strip().casefold()
        if not key:
            raise ValueError(f"filters[{index}] key must be non-empty")
        if not value:
            raise ValueError(f"filters[{index}] value must be non-empty")
        if key == "repository":
            repositories.append(value)
            continue
        if key == "tag":
            tags.append(value)
            continue
        raise ValueError(
            f"Unsupported provider filter key {key!r}; supported keys: repository, tag"
        )

    return ProjectFilters(
        repositories=_dedupe_preserve_order(repositories),
        tags=_dedupe_preserve_order(tags),
    )


def _dedupe_preserve_order(values: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return tuple(deduped)


def _resolve_github_token(config: GithubProjectsV2TaskProviderConfig) -> str:
    if config.github_token:
        return config.github_token
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN is required to poll GitHub Projects V2")
    return token


def _run_gh_graphql(
    *,
    query: str,
    variables: dict[str, Any],
    github_token: str,
    gh_bin: str,
) -> dict[str, Any]:
    env = os.environ.copy()
    env["GITHUB_TOKEN"] = github_token
    env["GH_TOKEN"] = github_token
    args = [gh_bin, "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        if value is None:
            continue
        if isinstance(value, bool):
            encoded = "true" if value else "false"
            args.extend(["-F", f"{key}={encoded}"])
            continue
        if isinstance(value, int) and not isinstance(value, bool):
            args.extend(["-F", f"{key}={value}"])
            continue
        if isinstance(value, str):
            args.extend(["-f", f"{key}={value}"])
            continue
        raise TypeError(
            f"Unsupported GraphQL variable type for '{key}': {type(value).__name__}"
        )
    try:
        result = subprocess.run(
            args,
            check=True,
            capture_output=True,
            text=True,
            timeout=GH_API_TIMEOUT_SECONDS,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"gh api graphql timed out after {GH_API_TIMEOUT_SECONDS}s"
        ) from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip()
        raise RuntimeError(f"gh api graphql failed: {stderr}") from exc
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        snippet = (result.stdout or "").strip()
        if not snippet:
            snippet = "<empty output>"
        if len(snippet) > 500:
            snippet = f"{snippet[:500]}..."
        raise RuntimeError(
            f"gh api graphql returned invalid JSON: {snippet}"
        ) from exc
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


def _extract_item_tags(item: dict[str, Any]) -> tuple[str, ...]:
    content = item.get("content") or {}
    labels = content.get("labels") or {}
    nodes = labels.get("nodes") or []
    tags: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        name = node.get("name")
        if not isinstance(name, str):
            continue
        normalized = name.strip().casefold()
        if not normalized:
            continue
        tags.append(normalized)
    return _dedupe_preserve_order(tags)


def _matches_filters(
    task: Task,
    item: dict[str, Any],
    filters: ProjectFilters,
) -> bool:
    if filters.is_empty():
        return True

    if filters.repositories:
        repository = (task.repo or "").strip().casefold()
        if repository not in filters.repositories:
            return False

    if filters.tags:
        item_tags = set(_extract_item_tags(item))
        if not all(tag in item_tags for tag in filters.tags):
            return False

    return True


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
