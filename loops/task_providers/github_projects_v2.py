from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from loops.state.approval_config import (
    DEFAULT_APPROVAL_COMMENT_PATTERN,
    normalize_approval_usernames,
)
from loops.state.provider_types import LoopsProviderConfig, SecretRequirement
from loops.state.run_record import Task
from loops.task_providers.base import TaskStatus

GITHUB_PROJECTS_V2_PROVIDER_ID = "github_projects_v2"
DEFAULT_GITHUB_PROJECTS_V2_PROJECT_URL = "https://github.com/orgs/YOUR_ORG/projects/1"
OwnerType = Literal["organization", "user"]
GH_API_TIMEOUT_SECONDS = 30
STATUS_FIELD_QUERY_PAGE_SIZE = 100
IN_PROGRESS_STATUS_NAMES = ("in progress",)
DONE_STATUS_NAMES = ("done", "completed", "complete")
TODO_STATUS_NAMES = ("todo", "to do", "backlog", "ready")


class GithubProjectsV2TaskProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    status_field: str = "Status"
    page_size: int = Field(default=50, gt=0)
    github_token: str | None = None
    filters: list[str] = Field(default_factory=list)
    approval_comment_usernames: list[str] = Field(default_factory=list)
    approval_comment_pattern: str = DEFAULT_APPROVAL_COMMENT_PATTERN
    allowlist: list[str] = Field(default_factory=list)


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


def build_default_provider_config_payload(
    *,
    project_url: str = DEFAULT_GITHUB_PROJECTS_V2_PROJECT_URL,
) -> dict[str, Any]:
    """Build a default JSON payload for GitHub Projects V2 provider_config."""

    defaults = GithubProjectsV2TaskProviderConfig(url=project_url)
    return {
        "url": defaults.url,
        "status_field": defaults.status_field,
        "page_size": defaults.page_size,
        "approval_comment_usernames": list(defaults.approval_comment_usernames),
        "approval_comment_pattern": defaults.approval_comment_pattern,
        "allowlist": list(defaults.allowlist),
    }


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


@dataclass(frozen=True)
class ProjectStatusField:
    project_id: str
    field_id: str
    option_ids_by_name: dict[str, str]
    option_names_by_id: dict[str, str]


def parse_project_url(url: str) -> ProjectLocator:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc not in {"github.com", "www.github.com"}:
        raise ValueError(
            "Project URL must look like https://github.com/orgs/<org>/projects/<number>"
        )
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

_ORG_STATUS_FIELD_QUERY = """
query($login: String!, $number: Int!, $fieldsFirst: Int!) {
  organization(login: $login) {
    projectV2(number: $number) {
      id
      fields(first: $fieldsFirst) {
        nodes {
          __typename
          ... on ProjectV2SingleSelectField {
            id
            name
            options {
              id
              name
            }
          }
        }
      }
    }
  }
}
"""

_USER_STATUS_FIELD_QUERY = """
query($login: String!, $number: Int!, $fieldsFirst: Int!) {
  user(login: $login) {
    projectV2(number: $number) {
      id
      fields(first: $fieldsFirst) {
        nodes {
          __typename
          ... on ProjectV2SingleSelectField {
            id
            name
            options {
              id
              name
            }
          }
        }
      }
    }
  }
}
"""

_SET_STATUS_MUTATION = """
mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
  updateProjectV2ItemFieldValue(
    input: {
      projectId: $projectId
      itemId: $itemId
      fieldId: $fieldId
      value: {singleSelectOptionId: $optionId}
    }
  ) {
    projectV2Item {
      id
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
        self.locator = parse_project_url(self.config.url)
        self.filters = _parse_filters(config.filters)
        self.approval_comment_usernames = normalize_approval_usernames(
            config.approval_comment_usernames
        )
        self.approval_comment_pattern = (
            config.approval_comment_pattern or DEFAULT_APPROVAL_COMMENT_PATTERN
        )
        self.review_actor_allowlist = normalize_approval_usernames(config.allowlist)
        self._status_field_cache: ProjectStatusField | None = None

    def poll(self, limit: int | None = None) -> list[Task]:
        if limit is not None and limit <= 0:
            return []

        github_token = _resolve_github_token(self.config)
        tasks: list[Task] = []
        after: str | None = None
        while True:
            response = _run_gh_graphql(
                query=_select_query(self.locator.owner_type),
                variables={
                    "login": self.locator.login,
                    "number": self.locator.number,
                    "after": after,
                    "first": self.config.page_size,
                    "statusField": self.config.status_field,
                },
                github_token=github_token,
                gh_bin=self.gh_bin,
            )
            items, page_info = _extract_items(response, self.locator.owner_type)
            for item in items:
                task = _map_item_to_task(item)
                if task is None:
                    continue
                if not _matches_filters(task, item, self.filters):
                    continue
                tasks.append(task)
            has_next_page = bool(page_info.get("hasNextPage"))
            if not has_next_page:
                break
            after = page_info.get("endCursor")
            if not after:
                raise RuntimeError("Pagination missing endCursor while hasNextPage=true")
        ordered_tasks = sorted(tasks, key=_task_oldest_first_key)
        if limit is None:
            return ordered_tasks
        return ordered_tasks[:limit]

    def update_status(self, task_id: str, status: TaskStatus) -> None:
        normalized_task_id = task_id.strip()
        if not normalized_task_id:
            raise ValueError("task_id must be a non-empty string")

        github_token = _resolve_github_token(self.config)
        item_id, current_status_name = self._find_project_item_status(
            task_id=normalized_task_id,
            github_token=github_token,
        )
        status_field = self._load_status_field(github_token=github_token)
        target_option_id, target_option_name = _resolve_target_status_option(
            status_field=status_field,
            status=status,
        )

        current_normalized = current_status_name.strip().casefold()
        if current_normalized and current_normalized == target_option_name.casefold():
            return

        _set_project_item_status(
            project_id=status_field.project_id,
            item_id=item_id,
            status_field_id=status_field.field_id,
            option_id=target_option_id,
            github_token=github_token,
            gh_bin=self.gh_bin,
        )

    def _find_project_item_status(
        self,
        *,
        task_id: str,
        github_token: str,
    ) -> tuple[str, str]:
        after: str | None = None
        while True:
            response = _run_gh_graphql(
                query=_select_query(self.locator.owner_type),
                variables={
                    "login": self.locator.login,
                    "number": self.locator.number,
                    "after": after,
                    "first": self.config.page_size,
                    "statusField": self.config.status_field,
                },
                github_token=github_token,
                gh_bin=self.gh_bin,
            )
            items, page_info = _extract_items(response, self.locator.owner_type)
            for item in items:
                content = item.get("content")
                if not isinstance(content, dict):
                    continue
                if str(content.get("id") or "") != task_id:
                    continue
                item_id = item.get("id")
                if not isinstance(item_id, str) or not item_id:
                    raise RuntimeError("project item id missing for matched task")
                return item_id, _extract_status_value(item.get("fieldValueByName"))
            has_next_page = bool(page_info.get("hasNextPage"))
            if not has_next_page:
                break
            after = page_info.get("endCursor")
            if not after:
                raise RuntimeError("Pagination missing endCursor while hasNextPage=true")
        raise RuntimeError(f"Task id {task_id!r} was not found in configured project")

    def _load_status_field(self, *, github_token: str) -> ProjectStatusField:
        if self._status_field_cache is not None:
            return self._status_field_cache
        response = _run_gh_graphql(
            query=_select_status_field_query(self.locator.owner_type),
            variables={
                "login": self.locator.login,
                "number": self.locator.number,
                "fieldsFirst": STATUS_FIELD_QUERY_PAGE_SIZE,
            },
            github_token=github_token,
            gh_bin=self.gh_bin,
        )
        status_field = _extract_project_status_field(
            payload=response,
            owner_type=self.locator.owner_type,
            status_field_name=self.config.status_field,
        )
        self._status_field_cache = status_field
        return status_field


def _select_query(owner_type: OwnerType) -> str:
    if owner_type == "organization":
        return _normalize_query(_ORG_QUERY)
    return _normalize_query(_USER_QUERY)


def _select_status_field_query(owner_type: OwnerType) -> str:
    if owner_type == "organization":
        return _normalize_query(_ORG_STATUS_FIELD_QUERY)
    return _normalize_query(_USER_STATUS_FIELD_QUERY)


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


def _set_project_item_status(
    *,
    project_id: str,
    item_id: str,
    status_field_id: str,
    option_id: str,
    github_token: str,
    gh_bin: str,
) -> None:
    _run_gh_graphql(
        query=_normalize_query(_SET_STATUS_MUTATION),
        variables={
            "projectId": project_id,
            "itemId": item_id,
            "fieldId": status_field_id,
            "optionId": option_id,
        },
        github_token=github_token,
        gh_bin=gh_bin,
    )


def _extract_items(
    payload: dict[str, Any],
    owner_type: OwnerType,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    data = payload.get("data")
    if not isinstance(data, dict):
        return [], {"hasNextPage": False, "endCursor": None}
    owner_key = "organization" if owner_type == "organization" else "user"
    owner = data.get(owner_key)
    if not isinstance(owner, dict):
        return [], {"hasNextPage": False, "endCursor": None}
    project = owner.get("projectV2")
    if not isinstance(project, dict):
        return [], {"hasNextPage": False, "endCursor": None}
    items = project.get("items")
    if not isinstance(items, dict):
        return [], {"hasNextPage": False, "endCursor": None}
    raw_nodes = items.get("nodes")
    if isinstance(raw_nodes, list):
        nodes = [node for node in raw_nodes if isinstance(node, dict)]
    else:
        nodes = []
    raw_page_info = items.get("pageInfo")
    if isinstance(raw_page_info, dict):
        page_info = raw_page_info
    else:
        page_info = {"hasNextPage": False, "endCursor": None}
    return nodes, page_info


def _extract_project_status_field(
    *,
    payload: dict[str, Any],
    owner_type: OwnerType,
    status_field_name: str,
) -> ProjectStatusField:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("missing data payload while resolving project status field")
    owner_key = "organization" if owner_type == "organization" else "user"
    owner = data.get(owner_key)
    if not isinstance(owner, dict):
        raise RuntimeError("missing project owner payload while resolving status field")
    project = owner.get("projectV2")
    if not isinstance(project, dict):
        raise RuntimeError("project not found while resolving status field")
    project_id = project.get("id")
    if not isinstance(project_id, str) or not project_id:
        raise RuntimeError("project id missing while resolving status field")
    fields = ((project.get("fields") or {}).get("nodes") or [])
    if not isinstance(fields, list):
        raise RuntimeError("project fields payload missing while resolving status field")
    target_field: dict[str, Any] | None = None
    for field in fields:
        if not isinstance(field, dict):
            continue
        raw_name = field.get("name")
        if not isinstance(raw_name, str):
            continue
        if raw_name.strip().casefold() == status_field_name.strip().casefold():
            target_field = field
            break
    if target_field is None:
        raise RuntimeError(f"project field {status_field_name!r} was not found")
    field_id = target_field.get("id")
    if not isinstance(field_id, str) or not field_id:
        raise RuntimeError("status field id missing while resolving status field")
    raw_options = target_field.get("options")
    if not isinstance(raw_options, list) or not raw_options:
        raise RuntimeError("status field options missing while resolving status field")
    option_ids_by_name: dict[str, str] = {}
    option_names_by_id: dict[str, str] = {}
    for option in raw_options:
        if not isinstance(option, dict):
            continue
        option_id = option.get("id")
        option_name = option.get("name")
        if not isinstance(option_id, str) or not option_id:
            continue
        if not isinstance(option_name, str) or not option_name.strip():
            continue
        normalized_name = option_name.strip().casefold()
        option_ids_by_name[normalized_name] = option_id
        option_names_by_id[option_id] = option_name.strip()
    if not option_ids_by_name:
        raise RuntimeError("status field did not expose usable options")
    return ProjectStatusField(
        project_id=project_id,
        field_id=field_id,
        option_ids_by_name=option_ids_by_name,
        option_names_by_id=option_names_by_id,
    )


def _resolve_target_status_option(
    *,
    status_field: ProjectStatusField,
    status: TaskStatus,
) -> tuple[str, str]:
    if status == "IN_PROGRESS":
        candidates = IN_PROGRESS_STATUS_NAMES
    elif status == "DONE":
        candidates = DONE_STATUS_NAMES
    else:
        candidates = TODO_STATUS_NAMES
    for candidate in candidates:
        option_id = status_field.option_ids_by_name.get(candidate.casefold())
        if option_id is None:
            continue
        return option_id, status_field.option_names_by_id.get(option_id, candidate)
    available = ", ".join(sorted(status_field.option_ids_by_name))
    raise RuntimeError(
        f"could not map internal status {status!r}; available project options: {available}"
    )


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


def _task_oldest_first_key(task: Task) -> tuple[str, str, str]:
    return (task.created_at, task.updated_at, task.id)
