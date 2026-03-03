from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from loops.core.handoff_handlers import (
    GH_COMMENT_STATE_FILE,
    GHCommentHandoffHandler,
    HANDOFF_HANDLER_GH_COMMENT,
    HANDOFF_HANDLER_STDIN,
    HandoffResult,
    parse_github_issue_url,
    resolve_builtin_handoff_handler,
    validate_handoff_handler_name,
)
from loops.state.run_record import Task


def _task(
    *,
    provider_id: str = "github_projects_v2",
    url: str = "https://github.com/acme/api/issues/42",
) -> Task:
    return Task(
        provider_id=provider_id,
        id="42",
        title="Need handoff",
        status="ready",
        url=url,
        created_at="2026-02-19T00:00:00Z",
        updated_at="2026-02-19T00:00:00Z",
    )


def test_validate_handoff_handler_name_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="handoff_handler"):
        validate_handoff_handler_name("unknown_handler")


def test_parse_github_issue_url_rejects_non_issue_paths() -> None:
    with pytest.raises(ValueError, match="issues"):
        parse_github_issue_url("https://github.com/acme/api/pull/42")


def test_parse_github_issue_url_normalizes_query_and_fragment() -> None:
    parsed = parse_github_issue_url(
        "https://github.com/Acme/API/issues/42/?q=1#fragment"
    )

    assert parsed.owner == "Acme"
    assert parsed.repo == "API"
    assert parsed.number == 42
    assert parsed.issue_url == "https://github.com/Acme/API/issues/42"


def test_resolve_builtin_handoff_handler_rejects_provider_mismatch(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires provider_id='github_projects_v2'"):
        resolve_builtin_handoff_handler(
            HANDOFF_HANDLER_GH_COMMENT,
            run_dir=tmp_path,
            task=_task(provider_id="custom_provider"),
            stdin_handler=lambda _payload: "",
        )


def test_gh_comment_handler_posts_once_and_consumes_reply(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    task = _task()
    comments: list[dict[str, object]] = []
    posted_bodies: list[str] = []

    def fake_subprocess_run(args, **_kwargs):
        command = list(args)
        if command[:3] == ["gh", "issue", "comment"]:
            body = command[command.index("--body") + 1]
            posted_bodies.append(body)
            comments.append(
                {
                    "id": str(len(comments) + 1),
                    "body": body,
                    "createdAt": f"2026-02-19T00:00:0{len(comments) + 1}Z",
                }
            )
            return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")
        if command[:3] == ["gh", "issue", "view"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout=json.dumps({"comments": comments}),
                stderr="",
            )
        raise AssertionError(f"unexpected gh command: {command}")

    monkeypatch.setattr("loops.core.handoff_handlers.subprocess.run", fake_subprocess_run)

    handler = GHCommentHandoffHandler(run_dir=run_dir, task=task, environ={})

    payload = {"message": "Need manual decision", "context": {"scope": "release"}}
    first = handler(payload)
    assert first == HandoffResult.waiting()
    assert len(posted_bodies) == 1
    assert "/loops-reply <message>" in posted_bodies[0]

    second = handler(payload)
    assert second == HandoffResult.waiting()
    assert len(posted_bodies) == 1

    comments.append(
        {
            "id": "99",
            "body": "/loops-reply Continue with option A",
            "createdAt": "2026-02-19T00:00:59Z",
        }
    )
    third = handler(payload)
    assert third.status == "response"
    assert third.response == "Continue with option A"
    assert len(posted_bodies) == 1

    fourth = handler(payload)
    assert fourth == HandoffResult.waiting()
    assert len(posted_bodies) == 1

    state_payload = json.loads((run_dir / GH_COMMENT_STATE_FILE).read_text())
    assert state_payload["last_consumed_reply_comment_id"] == "99"
    assert state_payload["payload_hash"]


def test_resolve_builtin_stdin_handler_wraps_string_response(tmp_path: Path) -> None:
    handler = resolve_builtin_handoff_handler(
        HANDOFF_HANDLER_STDIN,
        run_dir=tmp_path,
        task=_task(),
        stdin_handler=lambda _payload: "  ship it  ",
    )

    result = handler({"message": "hello"})
    assert result.status == "response"
    assert result.response == "  ship it  "


def test_gh_comment_handler_surfaces_timeout_errors(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    handler = GHCommentHandoffHandler(run_dir=run_dir, task=_task(), environ={})

    def fake_subprocess_run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(
            cmd=["gh", "issue", "view"],
            timeout=30,
            output="partial output",
            stderr="partial error",
        )

    monkeypatch.setattr("loops.core.handoff_handlers.subprocess.run", fake_subprocess_run)

    with pytest.raises(RuntimeError, match="timed out"):
        handler._run_gh(["gh", "issue", "view", "https://github.com/acme/api/issues/42"])
