from __future__ import annotations

"""Helpers for cleaning Loops runtime run directories."""

from dataclasses import dataclass
import json
import shutil
from pathlib import Path

import click

from loops.state.constants import (
    AGENT_LOG_FILE_NAME,
    ARCHIVE_DIR_NAME,
    INNER_LOOP_RUNS_DIR_NAME,
    RUN_LOG_FILE_NAME,
    RUN_RECORD_FILE_NAME,
)

RUN_LOG_FILE = RUN_LOG_FILE_NAME
AGENT_LOG_FILE = AGENT_LOG_FILE_NAME
RUN_RECORD_FILE = RUN_RECORD_FILE_NAME
ACTIVE_RUN_STATES = {"RUNNING", "WAITING_ON_REVIEW", "NEEDS_INPUT", "PR_APPROVED"}


@dataclass(frozen=True)
class ArchiveMove:
    source: Path
    destination: Path


@dataclass(frozen=True)
class CleanPlan:
    loops_root: Path
    runs_root: Path
    archive_root: Path
    delete_runs: tuple[Path, ...]
    archive_moves: tuple[ArchiveMove, ...]


@click.command("clean")
@click.option(
    "--loops-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path(".loops"),
    show_default=True,
    help="Directory where Loops runtime state and runs are stored.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Report cleanup actions without deleting or archiving any run directories.",
)
def clean_command(loops_root: Path, dry_run: bool) -> None:
    """Delete empty runs and archive completed runs under a Loops root."""

    try:
        plan = build_clean_plan(loops_root)
        execute_clean_plan(plan, dry_run=dry_run)
    except OSError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(format_clean_report(plan, dry_run=dry_run))


def build_clean_plan(loops_root: Path) -> CleanPlan:
    resolved_loops_root = loops_root.resolve()
    runs_root = resolved_loops_root / INNER_LOOP_RUNS_DIR_NAME
    archive_root = resolved_loops_root / ARCHIVE_DIR_NAME

    if not runs_root.exists():
        return CleanPlan(
            loops_root=resolved_loops_root,
            runs_root=runs_root,
            archive_root=archive_root,
            delete_runs=(),
            archive_moves=(),
        )

    delete_runs: list[Path] = []
    archive_moves: list[ArchiveMove] = []
    reserved_names = _existing_archive_names(archive_root)

    run_dirs = sorted(path for path in runs_root.iterdir() if path.is_dir())
    for run_dir in run_dirs:
        run_state = _read_run_state(run_dir)
        if _is_empty_run_dir(run_dir) and _can_delete_empty_run(run_state):
            delete_runs.append(run_dir)
            continue
        if run_state == "DONE":
            destination = _reserve_archive_destination(
                archive_root,
                run_dir.name,
                reserved_names,
            )
            archive_moves.append(
                ArchiveMove(
                    source=run_dir,
                    destination=destination,
                )
            )

    return CleanPlan(
        loops_root=resolved_loops_root,
        runs_root=runs_root,
        archive_root=archive_root,
        delete_runs=tuple(delete_runs),
        archive_moves=tuple(archive_moves),
    )


def execute_clean_plan(plan: CleanPlan, *, dry_run: bool) -> None:
    if dry_run:
        return

    for run_dir in plan.delete_runs:
        if run_dir.exists():
            shutil.rmtree(run_dir)

    if plan.archive_moves:
        plan.archive_root.mkdir(parents=True, exist_ok=True)
    for move in plan.archive_moves:
        if move.source.exists():
            move.source.rename(move.destination)


def format_clean_report(plan: CleanPlan, *, dry_run: bool) -> str:
    action_delete = "Would delete" if dry_run else "Deleted"
    action_archive = "Would archive" if dry_run else "Archived"

    lines = [
        f"Loops root: {plan.loops_root}",
        f"{action_delete} {len(plan.delete_runs)} empty run dir(s).",
    ]
    for run_dir in plan.delete_runs:
        lines.append(f"- {run_dir}")

    lines.append(f"{action_archive} {len(plan.archive_moves)} completed run dir(s).")
    for move in plan.archive_moves:
        lines.append(f"- {move.source} -> {move.destination}")

    if not plan.delete_runs and not plan.archive_moves:
        lines.append("No cleanup actions found.")
    return "\n".join(lines)


def _is_empty_run_dir(run_dir: Path) -> bool:
    run_log = run_dir / RUN_LOG_FILE
    agent_log = run_dir / AGENT_LOG_FILE
    if not run_log.is_file() or not agent_log.is_file():
        return False
    try:
        return run_log.stat().st_size == 0 and agent_log.stat().st_size == 0
    except OSError:
        return False


def _read_run_state(run_dir: Path) -> str | None:
    run_record_path = run_dir / RUN_RECORD_FILE
    if not run_record_path.is_file():
        return None
    try:
        payload = json.loads(run_record_path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return "INVALID"
    if not isinstance(payload, dict):
        return "INVALID"
    raw_state = payload.get("last_state")
    if not isinstance(raw_state, str):
        return "INVALID"
    normalized_state = raw_state.strip().upper()
    if not normalized_state:
        return "INVALID"
    return normalized_state


def _existing_archive_names(archive_root: Path) -> set[str]:
    if not archive_root.exists():
        return set()
    return {entry.name for entry in archive_root.iterdir()}


def _reserve_archive_destination(
    archive_root: Path,
    original_name: str,
    reserved_names: set[str],
) -> Path:
    if original_name not in reserved_names:
        reserved_names.add(original_name)
        return archive_root / original_name

    suffix = 1
    while True:
        candidate = f"{original_name}-{suffix}"
        if candidate not in reserved_names:
            reserved_names.add(candidate)
            return archive_root / candidate
        suffix += 1


def _can_delete_empty_run(run_state: str | None) -> bool:
    if run_state is None:
        return True
    if run_state == "DONE":
        return True
    if run_state in ACTIVE_RUN_STATES:
        return False
    return False
