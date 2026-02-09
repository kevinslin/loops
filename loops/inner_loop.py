from __future__ import annotations

"""Inner loop runner for executing Codex with the unified prompt."""

import argparse
import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Optional

from loops.run_record import CodexSession, RunRecord, read_run_record, write_run_record

PROMPT_TEMPLATE = (
    "Use dev.do to implement the task, open a PR, wait for review, address feedback, "
    "and cleanup when approved.\n"
    "Task: {task}\n"
)

SESSION_ID_PATTERN = re.compile(r"session[_\s-]*id\s*[:=]\s*([\w-]+)", re.IGNORECASE)
UUID_PATTERN = re.compile(
    r"\\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\\b",
    re.IGNORECASE,
)


def run_inner_loop(run_dir: Path, *, prompt_file: Optional[Path] = None) -> RunRecord:
    """Run the inner loop for a single task directory."""

    run_dir = run_dir.resolve()
    run_log = run_dir / "run.log"
    run_record = read_run_record(run_dir / "run.json")
    base_prompt = _load_prompt_file(prompt_file)
    prompt = _build_prompt(run_record.task.url, base_prompt)
    command = _resolve_codex_command()
    output, exit_code = _run_codex(command, prompt)
    _append_log(run_log, output)

    session_id = _extract_session_id(output)
    if session_id is None and exit_code == 0:
        _append_log(run_log, "[loops] warning: no session id detected in codex output")

    if exit_code != 0:
        _append_log(run_log, f"[loops] codex exit code {exit_code}")

    needs_user_input = exit_code != 0
    codex_session = run_record.codex_session
    if session_id is not None:
        codex_session = CodexSession(id=session_id, last_prompt=prompt)

    updated = write_run_record(
        run_dir / "run.json",
        RunRecord(
            task=run_record.task,
            pr=run_record.pr,
            codex_session=codex_session,
            needs_user_input=needs_user_input,
            last_state=run_record.last_state,
            updated_at=run_record.updated_at,
        ),
    )
    return updated


def _resolve_codex_command() -> list[str]:
    raw_command = os.environ.get("CODEX_CMD", "codex exec --yolo")
    command = shlex.split(raw_command)
    if not command:
        raise ValueError("CODEX_CMD cannot be empty")
    return command


def _load_prompt_file(prompt_file: Optional[Path]) -> Optional[str]:
    if prompt_file is None:
        prompt_path = os.environ.get("LOOPS_PROMPT_FILE") or os.environ.get(
            "CODEX_PROMPT_FILE"
        )
        if prompt_path:
            prompt_file = Path(prompt_path)
    if prompt_file is None:
        return None
    if not prompt_file.is_file():
        raise FileNotFoundError(f"Prompt file not found: {prompt_file}")
    return prompt_file.read_text()


def _build_prompt(task_url: str, base_prompt: Optional[str]) -> str:
    prompt = PROMPT_TEMPLATE.format(task=task_url)
    if base_prompt:
        trimmed = base_prompt.rstrip()
        return f"{trimmed}\\n\\n{prompt}"
    return prompt


def _run_codex(command: list[str], prompt: str) -> tuple[str, int]:
    try:
        result = subprocess.run(
            command,
            input=prompt,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=os.environ.copy(),
            check=False,
        )
        return result.stdout or "", result.returncode
    except Exception as exc:  # pragma: no cover - defensive logging
        return f"[loops] codex invocation failed: {exc}", 1


def _append_log(path: Path, content: str) -> None:
    if not content:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(content)
        if not content.endswith("\\n"):
            handle.write("\\n")


def _extract_session_id(output: str) -> Optional[str]:
    for line in output.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            if "session_id" in payload:
                return str(payload["session_id"])
            if "session" in payload:
                return str(payload["session"])
    match = SESSION_ID_PATTERN.search(output)
    if match:
        return match.group(1)
    match = UUID_PATTERN.search(output)
    if match:
        return match.group(0)
    return None


def _resolve_run_dir(run_dir: Optional[str]) -> Path:
    if run_dir:
        return Path(run_dir)
    env_run_dir = os.environ.get("LOOPS_RUN_DIR")
    if env_run_dir:
        return Path(env_run_dir)
    raise SystemExit("LOOPS_RUN_DIR is required (or pass --run-dir)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a Loops inner loop task.")
    parser.add_argument(
        "--run-dir",
        type=str,
        default=None,
        help="Path to the run directory (defaults to LOOPS_RUN_DIR).",
    )
    parser.add_argument(
        "--prompt-file",
        type=str,
        default=None,
        help="Optional path to a base prompt file.",
    )
    args = parser.parse_args()
    run_dir = _resolve_run_dir(args.run_dir)
    prompt_file = Path(args.prompt_file) if args.prompt_file else None
    run_inner_loop(run_dir, prompt_file=prompt_file)


if __name__ == "__main__":
    main()
