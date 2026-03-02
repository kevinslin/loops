from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Literal, Mapping, Optional, cast

from loops.state.constants import VALID_CHECKOUT_MODES

RunState = Literal["RUNNING", "WAITING_ON_REVIEW", "NEEDS_INPUT", "PR_APPROVED", "DONE"]
ReviewStatus = Literal["open", "changes_requested", "approved"]
CIStatus = Literal["pending", "success", "failure"]
AutoApproveVerdict = Literal["none", "APPROVE", "REJECT", "ESCALATE"]
CheckoutMode = Literal["branch", "worktree"]
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
    ci_status: Optional[CIStatus] = None
    ci_last_checked_at: Optional[str] = None
    merged_at: Optional[str] = None
    last_checked_at: Optional[str] = None
    latest_review_submitted_at: Optional[str] = None
    review_addressed_at: Optional[str] = None

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "RunPR":
        return RunPR(
            url=str(data["url"]),
            number=data.get("number"),
            repo=data.get("repo"),
            review_status=data.get("review_status"),
            ci_status=data.get("ci_status"),
            ci_last_checked_at=data.get("ci_last_checked_at"),
            merged_at=data.get("merged_at"),
            last_checked_at=data.get("last_checked_at"),
            latest_review_submitted_at=data.get("latest_review_submitted_at"),
            review_addressed_at=data.get("review_addressed_at"),
        )

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"url": self.url}
        if self.number is not None:
            payload["number"] = self.number
        if self.repo is not None:
            payload["repo"] = self.repo
        if self.review_status is not None:
            payload["review_status"] = self.review_status
        if self.ci_status is not None:
            payload["ci_status"] = self.ci_status
        if self.ci_last_checked_at is not None:
            payload["ci_last_checked_at"] = self.ci_last_checked_at
        if self.merged_at is not None:
            payload["merged_at"] = self.merged_at
        if self.last_checked_at is not None:
            payload["last_checked_at"] = self.last_checked_at
        if self.latest_review_submitted_at is not None:
            payload["latest_review_submitted_at"] = self.latest_review_submitted_at
        if self.review_addressed_at is not None:
            payload["review_addressed_at"] = self.review_addressed_at
        return payload


@dataclass(frozen=True)
class RunAutoApprove:
    verdict: AutoApproveVerdict = "none"
    impact: Optional[int] = None
    risk: Optional[int] = None
    size: Optional[int] = None
    judged_at: Optional[str] = None
    summary: Optional[str] = None

    @staticmethod
    def _parse_score(raw: Any, *, key: str) -> Optional[int]:
        if raw is None:
            return None
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise TypeError(f'payload["auto_approve"]["{key}"] must be an integer')
        if raw < 1 or raw > 5:
            raise ValueError(f'payload["auto_approve"]["{key}"] must be between 1 and 5')
        return raw

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "RunAutoApprove":
        verdict_raw = data.get("verdict", "none")
        if verdict_raw is None:
            verdict: AutoApproveVerdict = "none"
        elif isinstance(verdict_raw, str):
            stripped = verdict_raw.strip()
            if not stripped:
                verdict = "none"
            elif stripped.lower() == "none":
                verdict = "none"
            else:
                normalized = stripped.upper()
                if normalized not in {"APPROVE", "REJECT", "ESCALATE"}:
                    raise ValueError(
                        'payload["auto_approve"]["verdict"] must be one of '
                        '"none", "APPROVE", "REJECT", or "ESCALATE"'
                    )
                verdict = normalized
        else:
            raise TypeError('payload["auto_approve"]["verdict"] must be a string')

        judged_at = data.get("judged_at")
        if judged_at is not None and not isinstance(judged_at, str):
            raise TypeError('payload["auto_approve"]["judged_at"] must be a string')

        summary = data.get("summary")
        if summary is not None and not isinstance(summary, str):
            raise TypeError('payload["auto_approve"]["summary"] must be a string')

        return RunAutoApprove(
            verdict=verdict,
            impact=RunAutoApprove._parse_score(data.get("impact"), key="impact"),
            risk=RunAutoApprove._parse_score(data.get("risk"), key="risk"),
            size=RunAutoApprove._parse_score(data.get("size"), key="size"),
            judged_at=judged_at,
            summary=summary,
        )

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"verdict": self.verdict}
        if self.impact is not None:
            payload["impact"] = self.impact
        if self.risk is not None:
            payload["risk"] = self.risk
        if self.size is not None:
            payload["size"] = self.size
        if self.judged_at is not None:
            payload["judged_at"] = self.judged_at
        if self.summary is not None:
            payload["summary"] = self.summary
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
    auto_approve: Optional[RunAutoApprove] = None
    needs_user_input_payload: Optional[Dict[str, Any]] = None
    stream_logs_stdout: Optional[bool] = None
    checkout_mode: CheckoutMode = "branch"
    starting_commit: str = "unknown"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task": self.task.to_dict(),
            "pr": self.pr.to_dict() if self.pr is not None else None,
            "codex_session": (
                self.codex_session.to_dict() if self.codex_session is not None else None
            ),
            "auto_approve": (
                self.auto_approve.to_dict() if self.auto_approve is not None else None
            ),
            "needs_user_input": self.needs_user_input,
            "needs_user_input_payload": self.needs_user_input_payload,
            "stream_logs_stdout": self.stream_logs_stdout,
            "checkout_mode": self.checkout_mode,
            "starting_commit": self.starting_commit,
            "last_state": self.last_state,
            "updated_at": self.updated_at,
        }


def derive_run_state(
    pr: Optional[RunPR],
    needs_user_input: bool,
    *,
    auto_approve_enabled: bool = False,
    auto_approve: Optional[RunAutoApprove] = None,
) -> RunState:
    if needs_user_input:
        return "NEEDS_INPUT"
    if pr is not None and pr.merged_at is not None:
        return "DONE"
    if pr is None:
        return "RUNNING"
    # Preserve the original manual approval path.
    if pr.review_status == "approved":
        return "PR_APPROVED"
    # Additional auto-approve path.
    if (
        auto_approve_enabled
        and pr.ci_status == "success"
        and auto_approve is not None
        and auto_approve.verdict == "APPROVE"
    ):
        return "PR_APPROVED"
    return "WAITING_ON_REVIEW"


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


def _validate_checkout_mode(value: Any, *, key: str) -> CheckoutMode:
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string")
    normalized = value.strip().casefold()
    if normalized not in VALID_CHECKOUT_MODES:
        raise ValueError(f"{key} must be one of: branch, worktree")
    return cast(CheckoutMode, normalized)


def read_run_record(path: str | Path) -> RunRecord:
    payload = json.loads(Path(path).read_text())
    needs_user_input = payload.get("needs_user_input")
    if not isinstance(needs_user_input, bool):
        raise TypeError('payload["needs_user_input"] must be a boolean')
    needs_user_input_payload = _validate_needs_user_input_payload(
        payload.get("needs_user_input_payload")
    )
    stream_logs_stdout = payload.get("stream_logs_stdout")
    if stream_logs_stdout is not None and not isinstance(stream_logs_stdout, bool):
        raise TypeError('payload["stream_logs_stdout"] must be a boolean or null')
    checkout_mode = _validate_checkout_mode(
        payload.get("checkout_mode", "branch"),
        key='payload["checkout_mode"]',
    )
    starting_commit = payload.get("starting_commit", "unknown")
    if not isinstance(starting_commit, str):
        raise TypeError('payload["starting_commit"] must be a string')
    return RunRecord(
        task=Task.from_dict(payload["task"]),
        pr=RunPR.from_dict(payload["pr"]) if payload.get("pr") else None,
        codex_session=(
            CodexSession.from_dict(payload["codex_session"])
            if payload.get("codex_session")
            else None
        ),
        auto_approve=(
            RunAutoApprove.from_dict(payload["auto_approve"])
            if payload.get("auto_approve")
            else None
        ),
        needs_user_input=needs_user_input,
        needs_user_input_payload=needs_user_input_payload,
        stream_logs_stdout=stream_logs_stdout,
        checkout_mode=checkout_mode,
        starting_commit=starting_commit.strip() or "unknown",
        last_state=payload["last_state"],
        updated_at=payload["updated_at"],
    )


def write_run_record(
    path: str | Path,
    record: RunRecord,
    *,
    auto_approve_enabled: bool = False,
) -> RunRecord:
    needs_user_input_payload = _validate_needs_user_input_payload(
        record.needs_user_input_payload
    )
    stream_logs_stdout = record.stream_logs_stdout
    if stream_logs_stdout is not None and not isinstance(stream_logs_stdout, bool):
        raise TypeError("record.stream_logs_stdout must be a boolean or null")
    checkout_mode = _validate_checkout_mode(
        record.checkout_mode,
        key="record.checkout_mode",
    )
    starting_commit = record.starting_commit
    if not isinstance(starting_commit, str):
        raise TypeError("record.starting_commit must be a string")
    updated_record = RunRecord(
        task=record.task,
        pr=record.pr,
        codex_session=record.codex_session,
        auto_approve=record.auto_approve,
        needs_user_input=record.needs_user_input,
        needs_user_input_payload=needs_user_input_payload,
        stream_logs_stdout=stream_logs_stdout,
        checkout_mode=checkout_mode,
        starting_commit=starting_commit.strip() or "unknown",
        last_state=derive_run_state(
            record.pr,
            record.needs_user_input,
            auto_approve_enabled=auto_approve_enabled,
            auto_approve=record.auto_approve,
        ),
        updated_at=_now_iso(),
    )
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(updated_record.to_dict(), indent=2, sort_keys=True))
    return updated_record
