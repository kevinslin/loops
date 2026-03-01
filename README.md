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

- `provider_id` (string, required): currently only `"github_projects_v2"`.
- `version` (integer, required for latest schema): config schema version. Legacy files without this field are treated as version `0`; latest is `1`.
- `provider_config` (object, required):
- `provider_config.url` (string, required): GitHub Projects V2 URL, for example `https://github.com/orgs/acme/projects/7`.
- `provider_config.status_field` (string, optional, default `"Status"`): Project field name to map task status.
- `provider_config.page_size` (integer, optional, default `50`): GraphQL page size.
- `provider_config.github_token` (string, optional): Overrides token used by the provider once launched.
- `provider_config.filters` (string[], optional): provider-side `key=value` filters. Supported keys:
  - `repository=<owner>/<repo>` (repeatable; multiple repository filters are OR)
  - `tag=<label-name>` (repeatable; multiple tag filters are AND)
- `provider_config` is validated by the provider's typed Pydantic model (unknown keys and invalid types fail fast).
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
    Requires `provider_id="github_projects_v2"` and `task.url` in issue URL format.
- `inner_loop` (object, optional when using outer CLI):
- `inner_loop.command` (string or string[], required when `inner_loop` is provided)
- `inner_loop.working_dir` (string, optional): relative paths are resolved from config directory.
- `inner_loop.env` (object string->string, optional): extra environment variables.
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
- Log timestamps are local-time strings without timezone suffix and use fixed fractional digits.
- With `emit_on_first_run=false`, first run initializes dedupe state but does not launch tasks.
- `--task-url` does not change `provider_config.url`; it selects one task after polling by URL match and runs only that task.
- `--task-url` forces foreground execution for that run (`sync_mode=true`) so targeted runs are interactive and deterministic.
- URL matching for `--task-url` compares normalized URLs (scheme/host case-insensitive, query/fragment removed, trailing slash ignored).
- `--task-url` bypasses ready-status filtering for the selected task and raises an error when the URL is missing or ambiguous in poll results.
- Provider filters (`provider_config.filters`) are applied during provider polling before outer-loop status filtering.
- `LOOPS_TASK_ID`, `LOOPS_TASK_TITLE`, `LOOPS_TASK_URL`, `LOOPS_TASK_PROVIDER`, `LOOPS_HANDOFF_HANDLER`, and `LOOPS_RUN_DIR` are injected into each launched inner-loop process.
- PR approval is detected from GitHub review decision or from allowlisted approval comments configured in `loop_config`.

### `loops doctor`

Upgrades `config.json` to the latest supported schema version and fills missing
`loop_config` keys with current defaults without overwriting existing values.

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
- `--prompt-file PATH`: Optional base prompt file. If omitted, inner loop checks `LOOPS_PROMPT_FILE`, then `CODEX_PROMPT_FILE`.
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
- If `LOOPS_STREAM_LOGS_STDOUT=1` (set automatically by outer loop in `sync_mode=true`), also mirrors `run.log` lines to stdout.
- Uses `CODEX_CMD` if set; default command is `codex exec --yolo`.
- Polls PR state with `gh pr view` when a PR is present.
- In review polling, Loops treats a PR as approved if `reviewDecision=APPROVED` or if a matching approval comment from `loop_config.approval_comment_usernames` is newer than the latest `CHANGES_REQUESTED` review.
- Applies pending signals from `state_signals.jsonl`.
- Selects handoff behavior from `LOOPS_HANDOFF_HANDLER`:
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
- `CODEX_CMD`: command used for Codex execution in inner loop. Default: `codex exec --yolo`.
- `LOOPS_RUN_DIR`: run directory for `loops.inner_loop` and `loops.state_signal` when `--run-dir` is not passed.
- `LOOPS_PROMPT_FILE` / `CODEX_PROMPT_FILE`: optional base prompt file for inner loop.
- `LOOPS_TASK_ID`, `LOOPS_TASK_TITLE`, `LOOPS_TASK_URL`, `LOOPS_TASK_PROVIDER`: set by outer loop when launching inner loops.
- `LOOPS_HANDOFF_HANDLER`: handoff strategy for inner loop (`stdin_handler` or `gh_comment_handler`). Outer loop sets this from `loop_config.handoff_handler`.

## Development

Run tests:

```sh
make test
```
