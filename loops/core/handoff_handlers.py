from __future__ import annotations

"""Built-in handoff handlers for inner-loop NEEDS_INPUT state."""

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from loops.task_providers.github_projects_v2 import GITHUB_PROJECTS_V2_PROVIDER_ID
from loops.state.run_record import Task

HANDOFF_HANDLER_STDIN = "stdin_handler"
HANDOFF_HANDLER_GH_COMMENT = "gh_comment_handler"
DEFAULT_HANDOFF_HANDLER = HANDOFF_HANDLER_STDIN
SUPPORTED_HANDOFF_HANDLERS = (
    HANDOFF_HANDLER_STDIN,
    HANDOFF_HANDLER_GH_COMMENT,
)
GH_COMMENT_STATE_FILE = "handoff_gh_comment_state.json"
LOOPS_REPLY_PREFIX = "/loops-reply"
PROMPT_MARKER_PREFIX = "<!-- loops-handoff"
ISSUE_PATH_PATTERN = re.compile(r"^/([^/]+)/([^/]+)/issues/([1-9][0-9]*)/?$")
REPLY_LINE_PATTERN = re.compile(r"^\s*/loops-reply\b(.*)$", re.IGNORECASE)


@dataclass(frozen=True)
class HandoffResult:
    """Represents a handoff outcome from a NEEDS_INPUT handler."""

    status: str
    response: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"waiting", "response"}:
            raise ValueError("HandoffResult.status must be 'waiting' or 'response'")
        if self.status == "response":
            if self.response is None or not self.response.strip():
                raise ValueError("HandoffResult.response is required for status='response'")
        elif self.response is not None:
            raise ValueError("HandoffResult.response must be null for status='waiting'")

    @staticmethod
    def waiting() -> "HandoffResult":
        return HandoffResult(status="waiting")

    @staticmethod
    def from_response(response: str) -> "HandoffResult":
        return HandoffResult(status="response", response=response)


@dataclass(frozen=True)
class IssueRef:
    owner: str
    repo: str
    number: int
    issue_url: str


@dataclass(frozen=True)
class IssueComment:
    comment_id: str
    body: str
    timestamp: str


@dataclass(frozen=True)
class ReplyComment:
    comment_id: str
    timestamp: str
    response: str


@dataclass(frozen=True)
class GHCommentHandoffState:
    payload_hash: str | None = None
    prompt_comment_id: str | None = None
    prompt_comment_timestamp: str | None = None
    last_consumed_reply_comment_id: str | None = None
    last_consumed_reply_timestamp: str | None = None

    @staticmethod
    def from_payload(payload: Mapping[str, Any]) -> "GHCommentHandoffState":
        return GHCommentHandoffState(
            payload_hash=_opt_str(payload.get("payload_hash")),
            prompt_comment_id=_opt_str(payload.get("prompt_comment_id")),
            prompt_comment_timestamp=_opt_str(payload.get("prompt_comment_timestamp")),
            last_consumed_reply_comment_id=_opt_str(
                payload.get("last_consumed_reply_comment_id")
            ),
            last_consumed_reply_timestamp=_opt_str(
                payload.get("last_consumed_reply_timestamp")
            ),
        )

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.payload_hash is not None:
            payload["payload_hash"] = self.payload_hash
        if self.prompt_comment_id is not None:
            payload["prompt_comment_id"] = self.prompt_comment_id
        if self.prompt_comment_timestamp is not None:
            payload["prompt_comment_timestamp"] = self.prompt_comment_timestamp
        if self.last_consumed_reply_comment_id is not None:
            payload["last_consumed_reply_comment_id"] = self.last_consumed_reply_comment_id
        if self.last_consumed_reply_timestamp is not None:
            payload["last_consumed_reply_timestamp"] = self.last_consumed_reply_timestamp
        return payload


def validate_handoff_handler_name(handler_name: str) -> str:
    """Validate and normalize a configured handoff handler name."""

    if not isinstance(handler_name, str):
        raise TypeError("loop_config.handoff_handler must be a string")
    normalized = handler_name.strip()
    if not normalized:
        raise ValueError("loop_config.handoff_handler cannot be empty")
    if normalized not in SUPPORTED_HANDOFF_HANDLERS:
        allowed = ", ".join(SUPPORTED_HANDOFF_HANDLERS)
        raise ValueError(
            f"loop_config.handoff_handler must be one of: {allowed} "
            f"(received {normalized!r})"
        )
    return normalized


def validate_handoff_handler_provider_compatibility(
    handler_name: str,
    provider_id: str,
) -> None:
    """Fail fast for unsupported handoff-handler/provider combinations."""

    validated_handler = validate_handoff_handler_name(handler_name)
    if validated_handler != HANDOFF_HANDLER_GH_COMMENT:
        return
    if provider_id == GITHUB_PROJECTS_V2_PROVIDER_ID:
        return
    raise ValueError(
        "loop_config.handoff_handler='gh_comment_handler' requires "
        f"provider_id='{GITHUB_PROJECTS_V2_PROVIDER_ID}', "
        f"received provider_id={provider_id!r}"
    )


def resolve_builtin_handoff_handler(
    handler_name: str,
    *,
    run_dir: Path,
    task: Task,
    stdin_handler: Callable[[dict[str, Any]], str],
    log_message: Callable[[str], None] | None = None,
    environ: Mapping[str, str] | None = None,
) -> Callable[[dict[str, Any]], HandoffResult]:
    """Resolve a configured built-in handoff handler."""

    validated = validate_handoff_handler_name(handler_name)
    if validated == HANDOFF_HANDLER_STDIN:
        return _wrap_stdin_handler(stdin_handler)
    return GHCommentHandoffHandler(
        run_dir=run_dir,
        task=task,
        log_message=log_message,
        environ=environ,
    )


def parse_github_issue_url(url: str) -> IssueRef:
    """Parse and validate a GitHub issue URL."""

    raw_url = url.strip()
    if not raw_url:
        raise ValueError("task URL cannot be empty for gh_comment_handler")
    parts = urlsplit(raw_url)
    if parts.scheme.casefold() not in {"http", "https"}:
        raise ValueError(
            "gh_comment_handler requires an absolute GitHub issue URL with http/https scheme"
        )
    if parts.netloc.casefold() != "github.com":
        raise ValueError(
            "gh_comment_handler requires a GitHub issue URL hosted on github.com"
        )
    path_match = ISSUE_PATH_PATTERN.match(parts.path)
    if path_match is None:
        raise ValueError(
            "gh_comment_handler requires task.url in the format "
            "https://github.com/<owner>/<repo>/issues/<number>"
        )
    owner = path_match.group(1)
    repo = path_match.group(2)
    issue_number = int(path_match.group(3))
    normalized_url = urlunsplit(
        (
            parts.scheme.casefold(),
            parts.netloc.casefold(),
            f"/{owner}/{repo}/issues/{issue_number}",
            "",
            "",
        )
    )
    return IssueRef(
        owner=owner,
        repo=repo,
        number=issue_number,
        issue_url=normalized_url,
    )


class GHCommentHandoffHandler:
    """Resolve NEEDS_INPUT handoff through GitHub issue comments."""

    def __init__(
        self,
        *,
        run_dir: Path,
        task: Task,
        log_message: Callable[[str], None] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        validate_handoff_handler_provider_compatibility(
            HANDOFF_HANDLER_GH_COMMENT,
            task.provider_id,
        )
        self._run_dir = run_dir.resolve()
        self._task = task
        self._issue = parse_github_issue_url(task.url)
        self._state_path = self._run_dir / GH_COMMENT_STATE_FILE
        self._run_key = self._run_dir.name
        self._log_message = log_message
        self._environ = dict(environ) if environ is not None else dict(os.environ)

    def __call__(self, payload: dict[str, Any]) -> HandoffResult:
        normalized_payload = _normalize_handoff_payload(payload)
        payload_hash = _hash_payload(normalized_payload)
        state = _read_gh_comment_state(self._state_path)

        if state.payload_hash != payload_hash:
            self._post_prompt_comment(normalized_payload, payload_hash)
            prompt_comment = self._find_latest_prompt_comment(payload_hash)
            if prompt_comment is None:
                raise RuntimeError(
                    "gh_comment_handler posted a handoff comment but could not locate it"
                )
            state = GHCommentHandoffState(
                payload_hash=payload_hash,
                prompt_comment_id=prompt_comment.comment_id,
                prompt_comment_timestamp=prompt_comment.timestamp,
                last_consumed_reply_comment_id=None,
                last_consumed_reply_timestamp=None,
            )
            _write_gh_comment_state(self._state_path, state)
            self._log(
                (
                    "[loops] gh_comment_handler posted handoff prompt "
                    f"issue={self._issue.issue_url} "
                    f"prompt_comment_id={prompt_comment.comment_id}"
                )
            )
            return HandoffResult.waiting()

        if state.prompt_comment_id is None or state.prompt_comment_timestamp is None:
            prompt_comment = self._find_latest_prompt_comment(payload_hash)
            if prompt_comment is None:
                self._post_prompt_comment(normalized_payload, payload_hash)
                prompt_comment = self._find_latest_prompt_comment(payload_hash)
            if prompt_comment is None:
                raise RuntimeError(
                    "gh_comment_handler could not locate handoff prompt comment"
                )
            state = replace(
                state,
                payload_hash=payload_hash,
                prompt_comment_id=prompt_comment.comment_id,
                prompt_comment_timestamp=prompt_comment.timestamp,
                last_consumed_reply_comment_id=None,
                last_consumed_reply_timestamp=None,
            )
            _write_gh_comment_state(self._state_path, state)

        reply = self._find_latest_reply_comment(state)
        if reply is None:
            self._log(
                (
                    "[loops] gh_comment_handler waiting for /loops-reply "
                    f"issue={self._issue.issue_url}"
                )
            )
            return HandoffResult.waiting()

        state = replace(
            state,
            last_consumed_reply_comment_id=reply.comment_id,
            last_consumed_reply_timestamp=reply.timestamp,
        )
        _write_gh_comment_state(self._state_path, state)
        self._log(
            (
                "[loops] gh_comment_handler received reply "
                f"issue={self._issue.issue_url} "
                f"reply_comment_id={reply.comment_id}"
            )
        )
        return HandoffResult.from_response(reply.response)

    def _post_prompt_comment(self, payload: Mapping[str, Any], payload_hash: str) -> None:
        body = self._build_prompt_body(payload, payload_hash)
        self._run_gh(
            [
                "gh",
                "issue",
                "comment",
                self._issue.issue_url,
                "--body",
                body,
            ]
        )

    def _build_prompt_body(self, payload: Mapping[str, Any], payload_hash: str) -> str:
        message = str(payload.get("message") or "").strip()
        context = payload.get("context")
        marker = self._prompt_marker(payload_hash)

        lines = [
            marker,
            "Loops needs input to continue this task.",
            "",
            message,
        ]
        if isinstance(context, Mapping) and context:
            lines.extend(
                [
                    "",
                    "Context:",
                    "```json",
                    json.dumps(dict(context), indent=2, sort_keys=True, ensure_ascii=True),
                    "```",
                ]
            )
        lines.extend(
            [
                "",
                f"Reply with `{LOOPS_REPLY_PREFIX} <message>` to continue.",
            ]
        )
        return "\n".join(lines).strip()

    def _find_latest_prompt_comment(self, payload_hash: str) -> IssueComment | None:
        marker = self._prompt_marker(payload_hash)
        latest: IssueComment | None = None
        for comment in self._list_issue_comments():
            if marker not in comment.body:
                continue
            if latest is None or _comment_sort_key(comment) > _comment_sort_key(latest):
                latest = comment
        return latest

    def _find_latest_reply_comment(self, state: GHCommentHandoffState) -> ReplyComment | None:
        latest_reply: ReplyComment | None = None
        for comment in self._list_issue_comments():
            if state.prompt_comment_timestamp is not None:
                prompt_key = _comment_sort_key_from_values(
                    state.prompt_comment_timestamp,
                    state.prompt_comment_id or "",
                )
                if _comment_sort_key(comment) <= prompt_key:
                    continue
            if (
                state.last_consumed_reply_timestamp is not None
                and _comment_sort_key(comment)
                <= _comment_sort_key_from_values(
                    state.last_consumed_reply_timestamp,
                    state.last_consumed_reply_comment_id or "",
                )
            ):
                continue
            response = _extract_reply_text(comment.body)
            if response is None:
                continue
            candidate = ReplyComment(
                comment_id=comment.comment_id,
                timestamp=comment.timestamp,
                response=response,
            )
            if latest_reply is None or _comment_sort_key_from_values(
                candidate.timestamp,
                candidate.comment_id,
            ) > _comment_sort_key_from_values(
                latest_reply.timestamp,
                latest_reply.comment_id,
            ):
                latest_reply = candidate
        return latest_reply

    def _list_issue_comments(self) -> list[IssueComment]:
        output = self._run_gh(
            [
                "gh",
                "issue",
                "view",
                self._issue.issue_url,
                "--json",
                "comments",
            ]
        )
        try:
            payload = json.loads(output)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "gh_comment_handler expected JSON output from `gh issue view`"
            ) from exc
        comments_payload = payload.get("comments")
        if not isinstance(comments_payload, list):
            raise RuntimeError("gh_comment_handler expected `comments` array from gh output")
        comments: list[IssueComment] = []
        for item in comments_payload:
            if not isinstance(item, Mapping):
                continue
            parsed = _parse_issue_comment(item)
            if parsed is not None:
                comments.append(parsed)
        return comments

    def _prompt_marker(self, payload_hash: str) -> str:
        return f"{PROMPT_MARKER_PREFIX} run={self._run_key} hash={payload_hash} -->"

    def _run_gh(self, args: Sequence[str]) -> str:
        result = subprocess.run(
            list(args),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env=self._environ,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        return result.stdout

    def _log(self, message: str) -> None:
        if self._log_message is None:
            return
        self._log_message(message)


def _wrap_stdin_handler(
    handler: Callable[[dict[str, Any]], str],
) -> Callable[[dict[str, Any]], HandoffResult]:
    def _wrapped(payload: dict[str, Any]) -> HandoffResult:
        return HandoffResult.from_response(str(handler(payload)))

    return _wrapped


def _normalize_handoff_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    message = payload.get("message")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("NEEDS_INPUT payload must include a non-empty message")
    normalized: dict[str, Any] = {"message": message.strip()}
    context = payload.get("context")
    if context is None:
        return normalized
    if not isinstance(context, Mapping):
        raise ValueError("NEEDS_INPUT payload context must be an object when provided")
    normalized["context"] = dict(context)
    return normalized


def _hash_payload(payload: Mapping[str, Any]) -> str:
    serialized = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _read_gh_comment_state(path: Path) -> GHCommentHandoffState:
    if not path.exists():
        return GHCommentHandoffState()
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return GHCommentHandoffState()
    if not isinstance(payload, Mapping):
        return GHCommentHandoffState()
    return GHCommentHandoffState.from_payload(payload)


def _write_gh_comment_state(path: Path, state: GHCommentHandoffState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.to_payload(), indent=2, sort_keys=True))


def _parse_issue_comment(payload: Mapping[str, Any]) -> IssueComment | None:
    body = payload.get("body")
    if not isinstance(body, str):
        return None
    timestamp = _extract_comment_timestamp(payload)
    if timestamp is None:
        return None
    comment_id = _extract_comment_id(payload, body=body, timestamp=timestamp)
    if comment_id is None:
        return None
    return IssueComment(
        comment_id=comment_id,
        body=body,
        timestamp=timestamp,
    )


def _extract_comment_id(
    payload: Mapping[str, Any],
    *,
    body: str,
    timestamp: str,
) -> str | None:
    for key in ("id", "databaseId", "url"):
        raw = payload.get(key)
        if isinstance(raw, int):
            return str(raw)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    digest = hashlib.sha256(f"{timestamp}\n{body}".encode("utf-8")).hexdigest()
    return digest[:16]


def _extract_comment_timestamp(payload: Mapping[str, Any]) -> str | None:
    updated_at = payload.get("updatedAt")
    if isinstance(updated_at, str) and updated_at:
        return updated_at
    created_at = payload.get("createdAt")
    if isinstance(created_at, str) and created_at:
        return created_at
    return None


def _extract_reply_text(body: str) -> str | None:
    lines = body.splitlines()
    if not lines:
        return None
    first_line = lines[0]
    match = REPLY_LINE_PATTERN.match(first_line)
    if match is None:
        return None
    inline_response = match.group(1).strip()
    if inline_response:
        return inline_response
    multiline_response = "\n".join(lines[1:]).strip()
    if multiline_response:
        return multiline_response
    return None


def _comment_sort_key(comment: IssueComment) -> tuple[str, int, int | str]:
    return _comment_sort_key_from_values(comment.timestamp, comment.comment_id)


def _comment_sort_key_from_values(
    timestamp: str,
    comment_id: str,
) -> tuple[str, int, int | str]:
    if comment_id.isdigit():
        return (timestamp, 0, int(comment_id))
    return (timestamp, 1, comment_id)


def _opt_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None
