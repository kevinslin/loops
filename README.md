# Loops

Loops is a lightweight task runner that picks up ready work from GitHub Projects and drives each task through an inner Codex loop until completion.

It has two runtime layers:
- An outer loop that polls tasks and launches runs.
- An inner loop that executes Codex turns, tracks PR/review state, and requests user input when needed.

## Requirements

- Python 3.10+
- `gh` CLI in `PATH`
- Codex CLI in `PATH` (or set `CODEX_CMD`)
- Python package: `click`

Install Python dependency:

```sh
python -m pip install click
```

## Quickstart

1. Initialize Loops:

```sh
python -m loops init
```

This creates `.loops/`, `.loops/jobs/`, `.loops/config.json`, `.loops/outer_state.json`, and `.loops/oloops.log`.

2. Provide GitHub auth for provider polling:

```sh
export GITHUB_TOKEN=YOUR_TOKEN
```

3. Run one poll cycle:

```sh
python -m loops run --run-once
```

4. For continuous polling:

```sh
python -m loops run
```

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
- `provider_config` (object, required):
- `provider_config.url` (string, required): GitHub Projects V2 URL, for example `https://github.com/orgs/acme/projects/7`.
- `provider_config.status_field` (string, optional, default `"Status"`): Project field name to map task status.
- `provider_config.page_size` (integer, optional, default `50`): GraphQL page size.
- `provider_config.github_token` (string, optional): Overrides `GITHUB_TOKEN`/`GH_TOKEN`.
- `loop_config` (object, optional):
- `loop_config.poll_interval_seconds` (integer, default `30`)
- `loop_config.parallel_tasks` (boolean, default `false`)
- `loop_config.parallel_tasks_limit` (integer, default `5`)
- `loop_config.sync_mode` (boolean, default `false`): run inner loop in foreground (interactive handoff enabled) instead of detached child.
- `loop_config.emit_on_first_run` (boolean, default `false`)
- `loop_config.force` (boolean, default `false`)
- `loop_config.task_ready_status` (string, default `"Ready"`)
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

### `python -m loops`

Wrapper CLI for all Loops interfaces.

Subcommands:

- `init`: initialize `.loops/` structure and default config.
- `run`: run the outer loop runner.
- `inner-loop`: run inner loop for one run directory.
- `signal`: enqueue a state signal for a run directory.

Examples:

```sh
python -m loops init
python -m loops run --run-once
python -m loops inner-loop --run-dir .loops/jobs/2026-02-09-example-task-123
python -m loops signal --run-dir .loops/jobs/2026-02-09-example-task-123 --message "Need approval"
```

Legacy compatibility:

- `python -m loops --run-once` is still accepted and is routed to `python -m loops run --run-once`.

### `python -m loops run`

Runs the outer loop runner.

Options:

- `--config PATH`: Config file path. Default: `.loops/config.json`.
- `--run-once / --run-forever`: Run one cycle then exit, or keep polling forever. Default: `--run-forever`.
- `--limit INTEGER`: Optional provider poll limit.
- `--force / --no-force`: Override `loop_config.force` from config.
- `-h, --help`: Show help.

Examples:

```sh
python -m loops run --run-once
python -m loops run --run-once --limit 3
python -m loops run --config /path/to/config.json --force
python -m loops run
```

Notes:

- Outer loop filters tasks by `loop_config.task_ready_status`.
- With `loop_config.sync_mode=true`, inner loop runs in the foreground and can prompt for user input in the same terminal.
- With `emit_on_first_run=false`, first run initializes dedupe state but does not launch tasks.
- `LOOPS_TASK_ID`, `LOOPS_TASK_TITLE`, `LOOPS_TASK_URL`, `LOOPS_TASK_PROVIDER`, and `LOOPS_RUN_DIR` are injected into each launched inner-loop process.

### `python -m loops inner-loop`

Runs the inner loop for a single run directory.

Options:

- `--run-dir PATH`: Run directory path. If omitted, uses `LOOPS_RUN_DIR`.
- `--prompt-file PATH`: Optional base prompt file. If omitted, inner loop checks `LOOPS_PROMPT_FILE`, then `CODEX_PROMPT_FILE`.
- `-h, --help`: Show help.

Examples:

```sh
python -m loops inner-loop --run-dir .loops/jobs/2026-02-14-test-issue-i-kwdoqyyzws7nwoys
LOOPS_RUN_DIR=.loops/jobs/2026-02-09-example-task-123 python -m loops inner-loop
LOOPS_RUN_DIR=.loops/jobs/2026-02-09-example-task-123 CODEX_CMD="codex exec --yolo" python -m loops inner-loop
```

Behavior summary:

- Reads and writes `run.json` as the authoritative state file.
- Writes inner-loop orchestration logs to `run.log`.
- Streams Codex/agent output to `agent.log`.
- Uses `CODEX_CMD` if set; default command is `codex exec --yolo`.
- Polls PR state with `gh pr view` when a PR is present.
- Applies pending signals from `state_signals.jsonl`.

### `python -m loops signal`

Enqueues state signals for an existing run directory. Current supported state: `NEEDS_INPUT`.

Options:

- `--run-dir PATH`: Run directory path. If omitted, uses `LOOPS_RUN_DIR`.
- `--state TEXT`: Signal state. Default: `NEEDS_INPUT`.
- `--message TEXT`: Required user-facing message.
- `--context JSON_OBJECT`: Optional JSON object payload context.
- `-h, --help`: Show help.

Examples:

```sh
python -m loops signal \
  --run-dir .loops/jobs/2026-02-09-example-task-123 \
  --message "Need approval to continue" \
  --context '{"reason":"scope_change"}'
```

Direct module equivalents still exist:

- `python -m loops.inner_loop`
- `python -m loops.state_signal`

Output on success:

```json
{"accepted": true, "signal": {"state": "NEEDS_INPUT", "payload": {"message": "...", "context": {}}, "created_at": "..."}}
```

## Environment variables

- `GITHUB_TOKEN` or `GH_TOKEN`: required for GitHub Projects polling unless `provider_config.github_token` is set.
- `CODEX_CMD`: command used for Codex execution in inner loop. Default: `codex exec --yolo`.
- `LOOPS_RUN_DIR`: run directory for `loops.inner_loop` and `loops.state_signal` when `--run-dir` is not passed.
- `LOOPS_PROMPT_FILE` / `CODEX_PROMPT_FILE`: optional base prompt file for inner loop.
- `LOOPS_TASK_ID`, `LOOPS_TASK_TITLE`, `LOOPS_TASK_URL`, `LOOPS_TASK_PROVIDER`: set by outer loop when launching inner loops.

## Development

Run tests:

```sh
make test
```
