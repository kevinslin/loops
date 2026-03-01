from __future__ import annotations

"""Helpers for cleaning Loops runtime run directories."""

from dataclasses import dataclass
import shutil
from pathlib import Path

from loops.outer_loop import INNER_LOOP_RUNS_DIR_NAME
from loops.run_record import read_run_record

RUN_LOG_FILE = "run.log"
AGENT_LOG_FILE = "agent.log"
RUN_RECORD_FILE = "run.json"
ARCHIVE_DIR_NAME = ".archive"


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
        if _is_empty_run_dir(run_dir):
            delete_runs.append(run_dir)
            continue
        if _is_completed_run_dir(run_dir):
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


def _is_completed_run_dir(run_dir: Path) -> bool:
    run_record_path = run_dir / RUN_RECORD_FILE
    if not run_record_path.is_file():
        return False
    try:
        run_record = read_run_record(run_record_path)
    except (KeyError, TypeError, ValueError):
        return False
    return run_record.last_state == "DONE"


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
