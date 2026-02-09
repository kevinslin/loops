from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Literal, Mapping, Optional

RunState = Literal["RUNNING", "WAITING_ON_REVIEW", "NEEDS_INPUT", "PR_APPROVED", "DONE"]
ReviewStatus = Literal["open", "changes_requested", "approved"]
MAX_NEEDS_USER_INPUT_PAYLOAD_BYTES = 16 * 1024


@dataclass(frozen=True)
class Task:
    provider_id: str
    id: str
    title: str
    status: str
    url: str
    created_at: str
    updated_at: str
    repo: Optional[str] = None

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "Task":
        return Task(
            provider_id=str(data["provider_id"]),
            id=str(data["id"]),
            title=str(data["title"]),
            status=str(data["status"]),
            url=str(data["url"]),
            created_at=str(data["created_at"]),
            updated_at=str(data["updated_at"]),
            repo=data.get("repo"),
        )

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "provider_id": self.provider_id,
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "url": self.url,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.repo is not None:
            payload["repo"] = self.repo
        return payload


@dataclass(frozen=True)
class RunPR:
    url: str
    number: Optional[int] = None
    repo: Optional[str] = None
    review_status: Optional[ReviewStatus] = None
    merged_at: Optional[str] = None
    last_checked_at: Optional[str] = None

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "RunPR":
        return RunPR(
            url=str(data["url"]),
            number=data.get("number"),
            repo=data.get("repo"),
            review_status=data.get("review_status"),
            merged_at=data.get("merged_at"),
            last_checked_at=data.get("last_checked_at"),
        )

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"url": self.url}
        if self.number is not None:
            payload["number"] = self.number
        if self.repo is not None:
            payload["repo"] = self.repo
        if self.review_status is not None:
            payload["review_status"] = self.review_status
        if self.merged_at is not None:
            payload["merged_at"] = self.merged_at
        if self.last_checked_at is not None:
            payload["last_checked_at"] = self.last_checked_at
        return payload


@dataclass(frozen=True)
class CodexSession:
    id: str
    last_prompt: Optional[str] = None

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "CodexSession":
        return CodexSession(
            id=str(data["id"]),
            last_prompt=data.get("last_prompt"),
        )

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"id": self.id}
        if self.last_prompt is not None:
            payload["last_prompt"] = self.last_prompt
        return payload


@dataclass(frozen=True)
class RunRecord:
    task: Task
    pr: Optional[RunPR]
    codex_session: Optional[CodexSession]
    needs_user_input: bool
    last_state: RunState
    updated_at: str
    needs_user_input_payload: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task": self.task.to_dict(),
            "pr": self.pr.to_dict() if self.pr is not None else None,
            "codex_session": (
                self.codex_session.to_dict() if self.codex_session is not None else None
            ),
            "needs_user_input": self.needs_user_input,
            "needs_user_input_payload": self.needs_user_input_payload,
            "last_state": self.last_state,
            "updated_at": self.updated_at,
        }


def derive_run_state(pr: Optional[RunPR], needs_user_input: bool) -> RunState:
    if needs_user_input:
        return "NEEDS_INPUT"
    if pr is not None and pr.merged_at is not None:
        return "DONE"
    if pr is not None and pr.review_status == "approved":
        return "PR_APPROVED"
    if pr is not None:
        return "WAITING_ON_REVIEW"
    return "RUNNING"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_needs_user_input_payload(
    payload: Any,
) -> Optional[Dict[str, Any]]:
    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        raise TypeError('payload["needs_user_input_payload"] must be an object or null')
    message = payload.get("message")
    if not isinstance(message, str) or not message.strip():
        raise TypeError(
            'payload["needs_user_input_payload"]["message"] must be a non-empty string'
        )
    context = payload.get("context")
    if context is not None and not isinstance(context, Mapping):
        raise TypeError(
            'payload["needs_user_input_payload"]["context"] must be an object when provided'
        )

    normalized: Dict[str, Any] = {"message": message.strip()}
    if context is not None:
        normalized["context"] = dict(context)

    serialized = json.dumps(normalized, ensure_ascii=True)
    if len(serialized.encode("utf-8")) > MAX_NEEDS_USER_INPUT_PAYLOAD_BYTES:
        raise ValueError("needs_user_input_payload exceeds max allowed size")
    return normalized


def read_run_record(path: str | Path) -> RunRecord:
    payload = json.loads(Path(path).read_text())
    needs_user_input = payload.get("needs_user_input")
    if not isinstance(needs_user_input, bool):
        raise TypeError('payload["needs_user_input"] must be a boolean')
    needs_user_input_payload = _validate_needs_user_input_payload(
        payload.get("needs_user_input_payload")
    )
    return RunRecord(
        task=Task.from_dict(payload["task"]),
        pr=RunPR.from_dict(payload["pr"]) if payload.get("pr") else None,
        codex_session=(
            CodexSession.from_dict(payload["codex_session"])
            if payload.get("codex_session")
            else None
        ),
        needs_user_input=needs_user_input,
        needs_user_input_payload=needs_user_input_payload,
        last_state=payload["last_state"],
        updated_at=payload["updated_at"],
    )


def write_run_record(path: str | Path, record: RunRecord) -> RunRecord:
    needs_user_input_payload = _validate_needs_user_input_payload(
        record.needs_user_input_payload
    )
    updated_record = RunRecord(
        task=record.task,
        pr=record.pr,
        codex_session=record.codex_session,
        needs_user_input=record.needs_user_input,
        needs_user_input_payload=needs_user_input_payload,
        last_state=derive_run_state(record.pr, record.needs_user_input),
        updated_at=_now_iso(),
    )
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(updated_record.to_dict(), indent=2, sort_keys=True))
    return updated_record
