# Agent Guidance

## Purpose
Document expectations and configuration for coding agents working in this repo.

## Usage
- Always refer to DESIGN.md before starting work.
- Use the active spec in docs/specs/active when present.

## Parameters and Configuration
- `.loops/config.json`: outer loop configuration (provider + loop + inner loop command).
- `LOOPS_RUN_DIR`: path to the inner loop run directory (required for inner loop runner).
- `CODEX_CMD`: command used to invoke Codex (default: `codex exec --yolo`).
- `LOOPS_PROMPT_FILE` / `CODEX_PROMPT_FILE`: optional base prompt file path.
- `LOOPS_TASK_ID`, `LOOPS_TASK_TITLE`, `LOOPS_TASK_URL`, `LOOPS_TASK_PROVIDER`: task metadata injected by the outer loop launcher.
