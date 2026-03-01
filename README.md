# Loops

Loops is a lightweight task runner that picks up ready work from GitHub Projects and drives each task through an inner Codex loop until completion.

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

`python -m loops ...` remains supported as an equivalent invocation.

## Runtime layout

Loops writes runtime state under `.loops/`:

```text
.loops/
  oloops.log
  outer_state.json
  jobs/
    YYYY-MM-DD-task-title-task-id/
      run.json
      run.log
      agent.log
      state_signals.jsonl
      state_signals.offset
```

## Configuration reference

Top-level config file: `.loops/config.json`

- `task_provider_id` (string, required): currently only `"github_projects_v2"`.
- `version` (integer, required for latest schema): config schema version. Legacy files without this field are treated as version `0`; latest is `2`.
- `task_provider_config` (object, required):
- `task_provider_config.url` (string, required): GitHub Projects V2 URL, for example `https://github.com/orgs/acme/projects/7`.
- `task_provider_config.status_field` (string, optional, default `"Status"`): Project field name to map task status.
- `task_provider_config.page_size` (integer, optional, default `50`): GraphQL page size.
- `task_provider_config.github_token` (string, optional): Overrides token used by the provider once launched.
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
- `loop_config.approval_comment_usernames` (string[], default `[]`): allowlisted GitHub usernames whose approval comments can mark a PR as approved.
- `loop_config.approval_comment_pattern` (string, default `^\s*/approve\b`): regex used to match approval comments from allowlisted usernames.
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

If `inner_loop` is omitted and you run via `python -m loops`, the CLI injects:
- `command = [sys.executable, "-m", "loops.inner_loop"]`
- `append_task_url = false`

## CLI reference

All CLI interfaces in this repo are listed below.

### `loops`

Wrapper CLI for all Loops interfaces.

Subcommands:

- `init`: initialize `.loops/` structure and default config.
- `run`: run the outer loop runner.
- `inner-loop`: run inner loop for one run directory.
- `signal`: enqueue a state signal for a run directory.
- `doctor`: upgrade config schema/default keys in `config.json`.

Examples:

```sh
loops init
loops doctor
loops run --run-once
loops inner-loop --run-dir .loops/jobs/2026-02-09-example-task-123
loops signal --run-dir .loops/jobs/2026-02-09-example-task-123 --message "Need approval"
```

Legacy compatibility:

- `loops --run-once` is accepted and routed to `loops run --run-once`.
- `python -m loops --run-once` is accepted and routed to `python -m loops run --run-once`.

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
- Outer loop always injects `LOOPS_RUN_DIR` into launched inner-loop processes. For non-`loops.inner_loop` custom launch commands, `inner_loop.env` is also merged into child env; for `loops.inner_loop` commands, runtime settings are read from run-scoped `inner_loop_runtime_config.json`.
- PR approval is detected from GitHub review decision or from allowlisted approval comments configured in `loop_config`, after optional review-actor filtering from `task_provider_config.allowlist`.

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
- Writes inner-loop orchestration logs to `run.log` and appends Codex output there.
- Streams Codex/agent output to `agent.log`.
- If run-scoped runtime config has `stream_logs_stdout=true` (written by outer loop in `sync_mode=true`), also mirrors `run.log` lines to stdout.
- Uses `CODEX_CMD` from run-scoped runtime config when present; if absent there, it falls back to process `CODEX_CMD`. Default command is `codex exec --yolo`.
- Polls PR state with `gh pr view` when a PR is present.
- In review polling, Loops treats a PR as approved if `reviewDecision=APPROVED` or if a matching approval comment from `loop_config.approval_comment_usernames` is newer than the latest `CHANGES_REQUESTED` review.
- If `task_provider_config.allowlist` is configured, review polling filters PR comments/reviews to those actors before deriving feedback and review-status signals.
- Applies pending signals from `state_signals.jsonl`.
- Selects handoff behavior from run-scoped runtime config (or `LOOPS_HANDOFF_HANDLER` for direct/manual runs):
  - `stdin_handler`: prompt directly in terminal.
  - `gh_comment_handler`: comment on task issue and wait for `/loops-reply ...`.
- `--reset` keeps task metadata and preserves an existing PR link (`pr.url`/`number`/`repo`) when present; non-link PR status fields are cleared.
- If `run.json` is missing, task fields fall back to `LOOPS_TASK_*` env vars (or defaults).
- Exact state-mapped prompt strings are documented in `DESIGN.md` under `### Prompt catalog`.

### `loops signal`

Enqueues state signals for an existing run directory. Current supported state: `NEEDS_INPUT`.

Options:

- `--run-dir PATH`: Run directory path. If omitted, uses `LOOPS_RUN_DIR`.
- `--state TEXT`: Signal state. Default: `NEEDS_INPUT`.
- `--message TEXT`: Required user-facing message.
- `--context JSON_OBJECT`: Optional JSON object payload context.
- `-h, --help`: Show help.

Examples:

```sh
loops signal \
  --run-dir .loops/jobs/2026-02-09-example-task-123 \
  --message "Need approval to continue" \
  --context '{"reason":"scope_change"}'
```

Direct module equivalents still exist:

- `python -m loops`
- `python -m loops.inner_loop`
- `python -m loops.state_signal`

Output on success:

```json
{"accepted": true, "signal": {"state": "NEEDS_INPUT", "payload": {"message": "...", "context": {}}, "created_at": "..."}}
```

## Environment variables

- `GITHUB_TOKEN` or `GH_TOKEN`: required for GitHub Projects provider startup checks (`GH_TOKEN` is accepted as alias fallback).
- `CODEX_CMD`: command used for Codex execution fallback when run-scoped runtime config does not set it. Default: `codex exec --yolo`.
- `LOOPS_RUN_DIR`: run directory for `loops.inner_loop` and `loops.state_signal` when `--run-dir` is not passed.
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

Live integration prerequisites:
- `gh` in `PATH` and authenticated with a token that can mutate the target project/repo.
- `codex` in `PATH` and authenticated.
- `GITHUB_TOKEN` or `GH_TOKEN` exported.

Notes:
- The live harness currently targets `https://github.com/users/kevinslin/projects/6/views/1`.
- Test issues are created in `kevinslin/loops-integ` and are cleaned up at test end.
- Pytest startup enforces this repository root at `sys.path[0]` (`tests/conftest.py`) so imports resolve to the active worktree checkout.
