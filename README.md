# Loops

Loops is a lightweight task runner that picks up ready work from GitHub Projects and drives each task through an inner Codex loop until completion.

> Warning
> Loops is a preview for trusted environments. It is not hardened for untrusted repositories, untrusted task content, or multi-tenant use.

It has two runtime layers:
- An outer loop that polls tasks and launches runs.
- An inner loop that executes Codex turns, tracks PR/review state, and requests user input when needed.

## Requirements

- Python 3.10+
- `gh` CLI in `PATH`
- Codex CLI in `PATH` (or set `CODEX_CMD`)
- Python package installer (`pip`)

Install Loops (editable) to get the `loops` command on your `PATH`:

```sh
python -m pip install -e .
```

## Quickstart

1. Initialize Loops:

```sh
loops init
```

This creates `.loops/`, `.loops/jobs/`, `.loops/config.json`, `.loops/outer_state.json`, and `.loops/oloops.log`.

2. Provide GitHub auth for provider polling:

```sh
export GITHUB_TOKEN=YOUR_TOKEN
```

3. Run one poll cycle:

```sh
loops run --run-once
```

4. For continuous polling:

```sh
loops run
```

## Runtime layout

Loops writes runtime state under `.loops/`:

```text
.loops/
  .archive/
    YYYY-MM-DD-task-title-task-id/
      run.json
      run.log
      agent.log
  oloops.log
  outer_state.json
  jobs/
    YYYY-MM-DD-task-title-task-id/
      run.json
      run.log
      agent.log
```

## Configuration reference

Top-level config file: `.loops/config.json`

- `task_provider_id` (string, required): currently only `"github_projects_v2"`.
- `version` (integer, required for latest schema): config schema version. Legacy files without this field are treated as version `0`; latest is `3`.
- `task_provider_config` (object, required):
- `task_provider_config.url` (string, required): GitHub Projects V2 URL, for example `https://github.com/orgs/acme/projects/7`.
- `task_provider_config.status_field` (string, optional, default `"Status"`): Project field name to map task status.
- `task_provider_config.page_size` (integer, optional, default `50`): GraphQL page size.
- `task_provider_config.github_token` (string, optional): Overrides token used by the provider once launched.
- `task_provider_config.approval_comment_usernames` (string[], optional, default `[]`): allowlisted GitHub usernames whose approval comments/review bodies can mark a PR as approved.
- `task_provider_config.approval_comment_pattern` (string, optional, default `^\s*/approve\b`): regex used to match approval comments/review bodies from `task_provider_config.approval_comment_usernames`.
- `task_provider_config.allowlist` (string[], optional, default `[]`): GitHub usernames allowed to contribute review-phase signals (PR comments/reviews) during inner-loop polling. When set, non-allowlisted actors are ignored in review polling.
- `task_provider_config.filters` (string[], optional): provider-side `key=value` filters. Supported keys:
  - `repository=<owner>/<repo>` (repeatable; multiple repository filters are OR)
  - `tag=<label-name>` (repeatable; multiple tag filters are AND)
- `task_provider_config` is validated by the provider's typed Pydantic model (unknown keys and invalid types fail fast).
- Required provider secrets are declared by provider metadata and validated from env before provider construction.
- `loop_config` (object, optional):
- `loop_config.poll_interval_seconds` (integer, default `30`)
- `loop_config.parallel_tasks` (boolean, default `false`)
- `loop_config.parallel_tasks_limit` (integer, default `5`)
- `loop_config.sync_mode` (boolean, default `false`): run inner loop in foreground (interactive handoff enabled) instead of detached child.
- `loop_config.emit_on_first_run` (boolean, default `false`)
- `loop_config.force` (boolean, default `false`)
- `loop_config.task_ready_status` (string, default `"Ready"`)
- `loop_config.auto_approve_enabled` (boolean, default `false`): enables one-time `$ag-judge` auto-approval evaluation when review is not already approved and CI is green.
- `loop_config.handoff_handler` (string, default `"stdin_handler"`): built-in NEEDS_INPUT handoff strategy. Supported values:
  - `stdin_handler`: prompt on stdin/stdout (interactive mode).
  - `gh_comment_handler`: post handoff prompts to the task GitHub issue and wait for `/loops-reply ...` comments.
    Requires `task_provider_id="github_projects_v2"` and `task.url` in issue URL format.
- `inner_loop` (object, optional when using outer CLI):
- `inner_loop.command` (string or string[], required when `inner_loop` is provided)
- `inner_loop.working_dir` (string, optional): relative paths are resolved from config directory.
- `inner_loop.env` (object string->string, optional): run-scoped inner-loop runtime env map (for example `CODEX_CMD`, prompt-file envs, or subprocess tokens) persisted to each run's `inner_loop_runtime_config.json`.
- `inner_loop.append_task_url` (boolean, default `true`)

If `inner_loop` is omitted, the CLI injects:
- `command` equivalent to `loops inner-loop`
- `append_task_url = false`

## CLI reference

All CLI interfaces in this repo are listed below.

### `loops`

Wrapper CLI for all Loops interfaces.

Subcommands:

- `init`: initialize `.loops/` structure and default config.
- `run`: run the outer loop runner.
- `inner-loop`: run inner loop for one run directory.
- `handoff`: seed a run from an existing Codex session and hand review-driving to Loops.
- `doctor`: upgrade config schema/default keys in `config.json`.
- `clean`: delete empty run directories and archive completed runs.

Examples:

```sh
loops init
loops doctor
loops run --run-once
loops inner-loop --run-dir .loops/jobs/2026-02-09-example-task-123
loops handoff 019cab59-95c2-7923-9049-be455a2beb37
loops clean --dry-run
```

Legacy compatibility:

- `loops --run-once` is accepted and routed to `loops run --run-once`.

### `loops run`

Runs the outer loop runner.

Options:

- `--config PATH`: Config file path. Default: `.loops/config.json`.
- `--run-once / --run-forever`: Run one cycle then exit, or keep polling forever. Default: `--run-forever`.
- `--limit INTEGER`: Optional provider poll limit.
- `--force / --no-force`: Override `loop_config.force` from config.
- `--task-url TEXT`: Force processing a specific task URL from provider results. This implies `--run-once`, `--force`, and `sync_mode=true`.
- `-h, --help`: Show help.

Examples:

```sh
loops run --run-once
loops run --run-once --limit 3
loops run --config /path/to/config.json --force
loops run --task-url https://github.com/acme/api/issues/123
loops run
```

Notes:

- Outer loop filters tasks by `loop_config.task_ready_status`.
- With `loop_config.sync_mode=true`, inner loop runs in the foreground and can prompt for user input in the same terminal.
- With `loop_config.sync_mode=true`, outer-loop logs (`oloops.log`) and inner-loop orchestration logs (`run.log`) are also mirrored to stdout.
- If `Ctrl+C` interrupts a sync-mode run, Loops prints resume instructions so you can continue with `loops inner-loop --run-dir ...`.
- Log timestamps are local-time strings without timezone suffix and use fixed fractional digits.
- With `emit_on_first_run=false`, first run initializes dedupe state but does not launch tasks.
- `--task-url` does not change `task_provider_config.url`; it selects one task after polling by URL match and runs only that task.
- `--task-url` forces foreground execution for that run (`sync_mode=true`) so targeted runs are interactive and deterministic.
- URL matching for `--task-url` compares normalized URLs (scheme/host case-insensitive, query/fragment removed, trailing slash ignored).
- `--task-url` bypasses ready-status filtering for the selected task and raises an error when the URL is missing or ambiguous in poll results.
- Provider filters (`task_provider_config.filters`) are applied during provider polling before outer-loop status filtering.
- Outer loop always injects `LOOPS_RUN_DIR` into launched inner-loop processes. For custom commands that are not `loops inner-loop`, `inner_loop.env` is also merged into child env; for Loops inner-loop commands, runtime settings are read from run-scoped `inner_loop_runtime_config.json`.
- PR approval is detected from GitHub review decision or from allowlisted approval comments configured in `task_provider_config`, after optional review-actor filtering from `task_provider_config.allowlist`.

### `loops clean`

Deletes empty runs and archives completed runs.

Options:

- `--loops-root PATH`: Loops runtime root. Default: `.loops`.
- `--dry-run`: Print planned delete/archive actions without changing files.
- `-h, --help`: Show help.

Examples:

```sh
loops clean --dry-run
loops clean
loops clean --loops-root /path/to/.loops
```

Behavior summary:

- A run is deleted when both `run.log` and `agent.log` exist and are byte-empty, and the run is not in an active state.
- A run is archived when `run.json` exists and `last_state == "DONE"`.
- `DONE` runs are always archived (never deleted as empty runs).
- Completed runs are moved to `.loops/.archive/`, and name collisions are resolved by appending `-1`, `-2`, etc.

### `loops doctor`

Upgrades `config.json` to the latest supported schema version and fills missing
`loop_config` keys (and GitHub `task_provider_config` defaults) without overwriting existing values.

Options:

- `--config PATH`: Config file path. Default: `.loops/config.json`.
- `-h, --help`: Show help.

Examples:

```sh
loops doctor
loops doctor --config /path/to/config.json
```

### `loops inner-loop`

Runs the inner loop for a single run directory.

Options:

- `--run-dir PATH`: Run directory path. If omitted, uses `LOOPS_RUN_DIR`.
- `--prompt-file PATH`: Optional base prompt file. If omitted, inner loop checks run-scoped runtime config (`inner_loop_runtime_config.json`) first, then falls back to `LOOPS_PROMPT_FILE`, then `CODEX_PROMPT_FILE`.
- `--reset`: Reset `run.json` orchestration/session/input state and exit.
- `-h, --help`: Show help.

Examples:

```sh
loops inner-loop --run-dir .loops/jobs/2026-02-14-test-issue-i-kwdoqyyzws7nwoys
loops inner-loop --run-dir .loops/jobs/2026-02-14-test-issue-i-kwdoqyyzws7nwoys --reset
LOOPS_RUN_DIR=.loops/jobs/2026-02-09-example-task-123 loops inner-loop
LOOPS_RUN_DIR=.loops/jobs/2026-02-09-example-task-123 CODEX_CMD="codex exec --yolo" loops inner-loop
```

Behavior summary:

- Reads and writes `run.json` as the authoritative state file.
- Persists `run.json.stream_logs_stdout` as the effective log-mirroring setting for the run.
- Writes inner-loop orchestration logs to `run.log` and appends Codex output there.
- Streams Codex/agent output to `agent.log`.
- If run-scoped runtime config has `stream_logs_stdout=true` (written by outer loop in `sync_mode=true`), also mirrors `run.log` lines to stdout.
- Uses `CODEX_CMD` from run-scoped runtime config when present; if absent there, it falls back to process `CODEX_CMD`. Default command is `codex exec --yolo`.
- Always sets `LOOPS_RUN_DIR` in the Codex subprocess env to the active run directory (even for direct/manual `loops inner-loop` runs).
- Polls PR state with `gh pr view` when a PR is present.
- For the initial PR, Loops expects the initial push sequence to run `scripts/push-pr.py` and write `${LOOPS_RUN_DIR}/push-pr.url`; after a successful `RUNNING` turn, inner loop reads that artifact only when `run.json.pr` is still missing, then populates `run.json.pr`.
- If `${LOOPS_RUN_DIR}/push-pr.url` is missing or invalid when `run.json.pr` is missing after a successful initial `RUNNING` turn, inner loop forces `NEEDS_INPUT` and includes the artifact path in `needs_user_input_payload.context`.
- In review polling, Loops treats a PR as approved if `reviewDecision=APPROVED` or if a matching approval comment from `task_provider_config.approval_comment_usernames` is newer than the latest `CHANGES_REQUESTED` review.
- If `task_provider_config.allowlist` is configured, review polling filters PR comments/reviews to those actors before deriving feedback and review-status signals.
- Selects handoff behavior from run-scoped runtime config (or `LOOPS_HANDOFF_HANDLER` for direct/manual runs):
  - `stdin_handler`: prompt directly in terminal.
  - `gh_comment_handler`: comment on task issue and wait for `/loops-reply ...`.
- `--reset` keeps task metadata and preserves an existing PR link (`pr.url`/`number`/`repo`) when present; non-link PR status fields are cleared.
- If `run.json` is missing, task fields fall back to `LOOPS_TASK_*` env vars (or defaults).
- Exact state-mapped prompt strings are documented in `DESIGN.md` under `### Prompt catalog`.

### `loops handoff`

Seeds and launches a `WAITING_ON_REVIEW` run from an existing Codex session.

Usage:

```sh
loops handoff [session-id]
```

Options:

- `--config PATH`: Config file path. Default: `.loops/config.json`.
- `--pr-url TEXT`: Optional PR URL override when transcript-based discovery is ambiguous or unavailable.
- `--task-url TEXT`: Optional tracking task URL override when transcript-based discovery is ambiguous or unavailable.
- `-h, --help`: Show help.

Behavior summary:

- Resolves session id from argument, then `CODEX_THREAD_ID`, then latest `${CODEX_HOME:-~/.codex}/history.jsonl` session.
- Reads the matching Codex transcript under `${CODEX_HOME:-~/.codex}/sessions` or `${CODEX_HOME:-~/.codex}/archived_sessions`.
- Derives PR URL and tracking task URL from conversation content; if either cannot be determined, prompts for it interactively (or fails with guidance in non-interactive mode).
- Maps tracking task URL against provider poll results when possible; otherwise falls back to synthesized task metadata.
- Creates a new run with:
  - `codex_session.id` set to the handed-off session id,
  - `pr.review_status="open"`,
  - `pr.review_addressed_at=null` (treat all existing PR feedback/comments as unread),
  - derived state `WAITING_ON_REVIEW`.
- Launches inner loop using the configured launcher path (`inner_loop.command`) and runtime config behavior.

## Environment variables

- `GITHUB_TOKEN` or `GH_TOKEN`: required for GitHub Projects provider startup checks (`GH_TOKEN` is accepted as alias fallback).
- `CODEX_CMD`: command used for Codex execution fallback when run-scoped runtime config does not set it. Default: `codex exec --yolo`.
- `LOOPS_RUN_DIR`: run directory for `loops inner-loop` when `--run-dir` is not passed.
- `LOOPS_RUN_DIR` is also required by `scripts/push-pr.py`, which writes deterministic PR URL discovery artifact `${LOOPS_RUN_DIR}/push-pr.url`.
- `LOOPS_PROMPT_FILE` / `CODEX_PROMPT_FILE`: optional base prompt file fallback when run-scoped runtime config does not set one.
- `LOOPS_HANDOFF_HANDLER`: handoff strategy fallback for direct/manual inner-loop runs (`stdin_handler` or `gh_comment_handler`).
- `LOOPS_TASK_ID`, `LOOPS_TASK_TITLE`, `LOOPS_TASK_URL`, `LOOPS_TASK_PROVIDER`: legacy fallback metadata used only when `run.json` is missing during `loops inner-loop --reset`.

## Development

Run tests:

```sh
make test
```

Run a targeted test module:

```sh
python -m pytest tests/test_outer_loop.py -q
```

Run live integration harness (opt-in):

```sh
LOOPS_INTEG_LIVE=1 python -m pytest tests/integ -k outer_loop_pickup_live -s
```

Run full live end-to-end lifecycle harness (opt-in, manual-only):

```sh
make test-integ-end2end
# equivalent:
LOOPS_INTEG_END2END=1 python -m pytest tests/integ -k end2end_live -s
```

Live integration prerequisites:
- `gh` in `PATH` and authenticated with a token that can mutate the target project/repo.
- `codex` in `PATH` and authenticated.
- `GITHUB_TOKEN` or `GH_TOKEN` exported.

Notes:
- The live harness currently targets `https://github.com/users/kevinslin/projects/6/views/1`.
- Test issues are created in `kevinslin/loops-integ` and are cleaned up at test end.
- The end-to-end harness also clones/syncs `.integ/loops-integ`, runs `loops run --run-once` with a 15-minute timeout, reverts merged changes in `kevinslin/loops-integ`, and runs `loops clean` for run-archive hygiene.
- End-to-end runs require push access to `kevinslin/loops-integ`.
- `make test-integ-end2end` is not part of the default `make test` suite.
- Pytest startup enforces this repository root at `sys.path[0]` (`tests/conftest.py`) so imports resolve to the active worktree checkout.
