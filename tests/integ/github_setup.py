from __future__ import annotations

import json
import os
import secrets
import subprocess
import time
from dataclasses import dataclass, replace
from typing import Any, Literal
from urllib.parse import urlparse

LOOPS_INTEG_PROJECT_URL = "https://github.com/users/kevinslin/projects/6/views/1"
LOOPS_INTEG_REPO = "kevinslin/loops-integ"
STATUS_FIELD_NAME = "Status"
READY_STATUS_NAME = "Todo"
NON_READY_STATUS_CANDIDATES = ("Backlog", "Todo", "To Do", "In Progress")
LABEL_COLOR = "1d76db"
DEFAULT_GH_TIMEOUT_SECONDS = 60.0

OwnerType = Literal["user", "organization"]


USER_PROJECT_QUERY = """
query($login: String!, $number: Int!) {
  user(login: $login) {
    projectV2(number: $number) {
      id
      fields(first: 100) {
        nodes {
          __typename
          ... on ProjectV2Field {
            id
            name
          }
          ... on ProjectV2SingleSelectField {
            id
            name
            options {
              id
              name
            }
          }
          ... on ProjectV2IterationField {
            id
            name
          }
        }
      }
    }
  }
}
"""


ORG_PROJECT_QUERY = """
query($login: String!, $number: Int!) {
  organization(login: $login) {
    projectV2(number: $number) {
      id
      fields(first: 100) {
        nodes {
          __typename
          ... on ProjectV2Field {
            id
            name
          }
          ... on ProjectV2SingleSelectField {
            id
            name
            options {
              id
              name
            }
          }
          ... on ProjectV2IterationField {
            id
            name
          }
        }
      }
    }
  }
}
"""


ADD_ITEM_MUTATION = """
mutation($projectId: ID!, $contentId: ID!) {
  addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
    item {
      id
    }
  }
}
"""


SET_FIELD_VALUE_MUTATION = """
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


DELETE_ITEM_MUTATION = """
mutation($projectId: ID!, $itemId: ID!) {
  deleteProjectV2Item(input: {projectId: $projectId, itemId: $itemId}) {
    deletedItemId
  }
}
"""


@dataclass(frozen=True)
class ProjectLocator:
    owner_type: OwnerType
    login: str
    number: int


@dataclass(frozen=True)
class ProjectMetadata:
    project_id: str
    status_field_id: str
    ready_option_id: str
    non_ready_option_id: str


@dataclass(frozen=True)
class IssueHandle:
    number: int
    node_id: str
    url: str
    title: str
    item_id: str | None = None


@dataclass(frozen=True)
class LiveIssueBundle:
    run_label: str
    task1: IssueHandle
    task2: IssueHandle
    project: ProjectMetadata


def require_github_token() -> str:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token is None or not token.strip():
        raise RuntimeError("GITHUB_TOKEN or GH_TOKEN is required for live integration tests")
    return token


def parse_project_url(url: str) -> ProjectLocator:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 4:
        raise ValueError(
            "Project URL must look like https://github.com/users/<login>/projects/<number>"
        )
    scope, login, projects_keyword, number = parts[0], parts[1], parts[2], parts[3]
    if scope not in {"users", "orgs"} or projects_keyword != "projects":
        raise ValueError(
            "Project URL must look like https://github.com/users/<login>/projects/<number>"
        )
    try:
        parsed_number = int(number)
    except ValueError as exc:
        raise ValueError("Project URL must end with a numeric project id") from exc
    if parsed_number <= 0:
        raise ValueError("Project id must be positive")
    owner_type: OwnerType = "user" if scope == "users" else "organization"
    return ProjectLocator(owner_type=owner_type, login=login, number=parsed_number)


def create_live_issue_bundle(
    *,
    token: str,
    project_url: str = LOOPS_INTEG_PROJECT_URL,
    repo: str = LOOPS_INTEG_REPO,
) -> LiveIssueBundle:
    locator = parse_project_url(project_url)
    project = fetch_project_metadata(locator, token=token)
    run_label = build_run_label()
    created_issues: list[IssueHandle] = []
    created_item_ids: list[str] = []

    try:
        ensure_repo_label(repo=repo, label=run_label, token=token)

        task1 = create_issue(
            repo=repo,
            title=f"[loops integ] task1 hello ({run_label})",
            body=(
                "Live Loops integration test task 1. "
                "Expected payload: add a test file 'hello'."
            ),
            label=run_label,
            token=token,
        )
        created_issues.append(task1)
        # Ensure distinct timestamps for oldest-first ordering tie-breaks.
        time.sleep(1.0)
        task2 = create_issue(
            repo=repo,
            title=f"[loops integ] task2 bye ({run_label})",
            body=(
                "Live Loops integration test task 2. "
                "Expected payload: add a test file 'bye'."
            ),
            label=run_label,
            token=token,
        )
        created_issues.append(task2)

        task1_item_id = add_issue_to_project(
            project_id=project.project_id,
            issue_node_id=task1.node_id,
            token=token,
        )
        created_item_ids.append(task1_item_id)
        task2_item_id = add_issue_to_project(
            project_id=project.project_id,
            issue_node_id=task2.node_id,
            token=token,
        )
        created_item_ids.append(task2_item_id)

        set_project_item_status(
            project_id=project.project_id,
            item_id=task1_item_id,
            status_field_id=project.status_field_id,
            option_id=project.ready_option_id,
            token=token,
        )
        set_project_item_status(
            project_id=project.project_id,
            item_id=task2_item_id,
            status_field_id=project.status_field_id,
            option_id=project.non_ready_option_id,
            token=token,
        )

        return LiveIssueBundle(
            run_label=run_label,
            task1=replace(task1, item_id=task1_item_id),
            task2=replace(task2, item_id=task2_item_id),
            project=project,
        )
    except Exception as exc:
        rollback_errors = _cleanup_partial_setup(
            project_id=project.project_id,
            repo=repo,
            issues=created_issues,
            item_ids=created_item_ids,
            token=token,
        )
        if rollback_errors:
            raise RuntimeError(
                "failed creating live issue bundle and partial cleanup failed: "
                f"setup_error={exc}; cleanup_errors={'; '.join(rollback_errors)}"
            ) from exc
        raise


def _cleanup_partial_setup(
    *,
    project_id: str,
    repo: str,
    issues: list[IssueHandle],
    item_ids: list[str],
    token: str,
) -> list[str]:
    errors: list[str] = []

    for item_id in item_ids:
        try:
            delete_project_item(project_id=project_id, item_id=item_id, token=token)
        except Exception as exc:  # pragma: no cover - defensive cleanup logging
            errors.append(f"failed deleting project item {item_id}: {exc}")

    for issue in issues:
        try:
            close_issue(repo=repo, issue_number=issue.number, token=token)
        except Exception as exc:  # pragma: no cover - defensive cleanup logging
            errors.append(f"failed closing issue {issue.number}: {exc}")

    return errors


def cleanup_live_issue_bundle(
    bundle: LiveIssueBundle,
    *,
    token: str,
    repo: str = LOOPS_INTEG_REPO,
) -> None:
    errors: list[str] = []

    for issue in (bundle.task1, bundle.task2):
        if issue.item_id is None:
            continue
        try:
            delete_project_item(
                project_id=bundle.project.project_id,
                item_id=issue.item_id,
                token=token,
            )
        except Exception as exc:  # pragma: no cover - defensive cleanup logging
            errors.append(f"failed deleting project item for issue {issue.number}: {exc}")

    for issue in (bundle.task1, bundle.task2):
        try:
            close_issue(repo=repo, issue_number=issue.number, token=token)
        except Exception as exc:  # pragma: no cover - defensive cleanup logging
            errors.append(f"failed closing issue {issue.number}: {exc}")

    if errors:
        raise RuntimeError("; ".join(errors))


def fetch_project_metadata(locator: ProjectLocator, *, token: str) -> ProjectMetadata:
    query = USER_PROJECT_QUERY if locator.owner_type == "user" else ORG_PROJECT_QUERY
    payload = graphql(
        query=query,
        variables={"login": locator.login, "number": locator.number},
        token=token,
    )
    data = payload.get("data") or {}
    owner_key = "user" if locator.owner_type == "user" else "organization"
    owner = data.get(owner_key) or {}
    project = owner.get("projectV2") or {}
    project_id = project.get("id")
    if not isinstance(project_id, str) or not project_id:
        raise RuntimeError("unable to resolve project id from GitHub project payload")

    fields = ((project.get("fields") or {}).get("nodes") or [])
    status_field = None
    for field in fields:
        if not isinstance(field, dict):
            continue
        if field.get("name") != STATUS_FIELD_NAME:
            continue
        status_field = field
        break
    if status_field is None:
        raise RuntimeError(f"project field {STATUS_FIELD_NAME!r} not found")

    status_field_id = status_field.get("id")
    if not isinstance(status_field_id, str) or not status_field_id:
        raise RuntimeError("status field id is missing")

    options = status_field.get("options")
    if not isinstance(options, list) or not options:
        raise RuntimeError("status field does not expose single-select options")

    option_by_name: dict[str, str] = {}
    for option in options:
        if not isinstance(option, dict):
            continue
        name = option.get("name")
        option_id = option.get("id")
        if isinstance(name, str) and isinstance(option_id, str):
            option_by_name[name.casefold()] = option_id

    ready_option_id = option_by_name.get(READY_STATUS_NAME.casefold())
    if ready_option_id is None:
        raise RuntimeError(f"status option {READY_STATUS_NAME!r} was not found")

    non_ready_option_id = select_non_ready_option_id(
        option_by_name=option_by_name,
        ready_option_id=ready_option_id,
    )

    return ProjectMetadata(
        project_id=project_id,
        status_field_id=status_field_id,
        ready_option_id=ready_option_id,
        non_ready_option_id=non_ready_option_id,
    )


def select_non_ready_option_id(*, option_by_name: dict[str, str], ready_option_id: str) -> str:
    for candidate in NON_READY_STATUS_CANDIDATES:
        option_id = option_by_name.get(candidate.casefold())
        if option_id is not None and option_id != ready_option_id:
            return option_id
    for option_id in option_by_name.values():
        if option_id != ready_option_id:
            return option_id
    raise RuntimeError("unable to find a non-ready status option")


def build_run_label() -> str:
    return f"loops-integ-{int(time.time())}-{secrets.token_hex(3)}"


def ensure_repo_label(*, repo: str, label: str, token: str) -> None:
    result = run_gh(
        [
            "api",
            f"repos/{repo}/labels",
            "-X",
            "POST",
            "-f",
            f"name={label}",
            "-f",
            f"color={LABEL_COLOR}",
            "-f",
            "description=Loops integration test run label",
        ],
        token=token,
        check=False,
    )
    if result.returncode == 0:
        return

    combined = f"{result.stdout}\n{result.stderr}"
    if "already_exists" in combined or "Validation Failed" in combined:
        return
    raise RuntimeError(format_gh_error(args=result.args, result=result))


def create_issue(
    *,
    repo: str,
    title: str,
    body: str,
    label: str,
    token: str,
) -> IssueHandle:
    payload = run_gh_json(
        [
            "api",
            f"repos/{repo}/issues",
            "-X",
            "POST",
            "-f",
            f"title={title}",
            "-f",
            f"body={body}",
            "-f",
            f"labels[]={label}",
        ],
        token=token,
    )
    number = payload.get("number")
    node_id = payload.get("node_id")
    url = payload.get("html_url")
    if not isinstance(number, int):
        raise RuntimeError("issue creation response missing number")
    if not isinstance(node_id, str) or not node_id:
        raise RuntimeError("issue creation response missing node_id")
    if not isinstance(url, str) or not url:
        raise RuntimeError("issue creation response missing html_url")
    return IssueHandle(number=number, node_id=node_id, url=url, title=title)


def close_issue(*, repo: str, issue_number: int, token: str) -> None:
    run_gh_json(
        [
            "api",
            f"repos/{repo}/issues/{issue_number}",
            "-X",
            "PATCH",
            "-f",
            "state=closed",
        ],
        token=token,
    )


def add_issue_to_project(*, project_id: str, issue_node_id: str, token: str) -> str:
    payload = graphql(
        query=ADD_ITEM_MUTATION,
        variables={"projectId": project_id, "contentId": issue_node_id},
        token=token,
    )
    item = (((payload.get("data") or {}).get("addProjectV2ItemById") or {}).get("item") or {})
    item_id = item.get("id")
    if not isinstance(item_id, str) or not item_id:
        raise RuntimeError("failed to add issue to project: item id missing")
    return item_id


def set_project_item_status(
    *,
    project_id: str,
    item_id: str,
    status_field_id: str,
    option_id: str,
    token: str,
) -> None:
    graphql(
        query=SET_FIELD_VALUE_MUTATION,
        variables={
            "projectId": project_id,
            "itemId": item_id,
            "fieldId": status_field_id,
            "optionId": option_id,
        },
        token=token,
    )


def delete_project_item(*, project_id: str, item_id: str, token: str) -> None:
    graphql(
        query=DELETE_ITEM_MUTATION,
        variables={"projectId": project_id, "itemId": item_id},
        token=token,
    )


def graphql(*, query: str, variables: dict[str, Any], token: str) -> dict[str, Any]:
    args = ["api", "graphql", "-f", f"query={normalize_query(query)}"]
    for key, value in variables.items():
        if value is None:
            continue
        if isinstance(value, bool):
            encoded = "true" if value else "false"
            args.extend(["-F", f"{key}={encoded}"])
        elif isinstance(value, int):
            args.extend(["-F", f"{key}={value}"])
        else:
            args.extend(["-f", f"{key}={value}"])

    payload = run_gh_json(args, token=token)
    errors = payload.get("errors")
    if errors:
        raise RuntimeError(f"GraphQL returned errors: {errors}")
    return payload


def normalize_query(query: str) -> str:
    return " ".join(query.split())


def run_gh_json(args: list[str], *, token: str) -> dict[str, Any]:
    result = run_gh(args, token=token, check=True)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        snippet = result.stdout.strip() or "<empty output>"
        if len(snippet) > 500:
            snippet = f"{snippet[:500]}..."
        raise RuntimeError(f"gh output was not JSON: {snippet}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("gh output JSON must be an object")
    return payload


def run_gh(
    args: list[str],
    *,
    token: str,
    check: bool,
) -> subprocess.CompletedProcess[str]:
    timeout_seconds = _resolve_gh_timeout_seconds()
    env = os.environ.copy()
    env["GITHUB_TOKEN"] = token
    env["GH_TOKEN"] = token
    command = ["gh", *args]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "gh command timed out "
            f"(timeout_seconds={timeout_seconds}) args={command!r}"
        ) from exc
    if check and result.returncode != 0:
        raise RuntimeError(format_gh_error(args=result.args, result=result))
    return result


def _resolve_gh_timeout_seconds() -> float:
    raw = os.environ.get("LOOPS_INTEG_GH_TIMEOUT_SECONDS", str(DEFAULT_GH_TIMEOUT_SECONDS))
    try:
        timeout_seconds = float(raw)
    except ValueError as exc:
        raise ValueError("LOOPS_INTEG_GH_TIMEOUT_SECONDS must be a number") from exc
    if timeout_seconds <= 0:
        raise ValueError("LOOPS_INTEG_GH_TIMEOUT_SECONDS must be > 0")
    return timeout_seconds


def format_gh_error(*, args: list[str], result: subprocess.CompletedProcess[str]) -> str:
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    return (
        "gh command failed "
        f"(exit_code={result.returncode}) "
        f"args={args!r} stdout={stdout!r} stderr={stderr!r}"
    )
