# Project layout for LLM agents

This file explains where important code lives and which files to edit for common changes.

## Quick orientation

Runtime flow:

1. `python -m loops` enters `loops/__main__.py`.
2. CLI parsing and subcommands are in `loops/cli.py`.
3. Outer-loop orchestration is in `loops/outer_loop.py`.
4. Per-task inner-loop orchestration is in `loops/inner_loop.py`.
5. Shared run-state schema and persistence are in `loops/run_record.py`.
6. Provider interface and GitHub provider implementation are in `loops/task_provider.py` and `loops/providers/github_projects_v2.py`.

## Repository map

```text
.
├── README.md
├── DESIGN.md
├── AGENTS.md
├── Makefile
├── LAYOUT.md
├── docs/
│   └── specs/
│       └── active/
├── loops/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli.py
│   ├── outer_loop.py
│   ├── inner_loop.py
│   ├── run_record.py
│   ├── task_provider.py
│   └── providers/
│       ├── __init__.py
│       └── github_projects_v2.py
└── tests/
    ├── test_cli.py
    ├── test_outer_loop.py
    ├── test_inner_loop.py
    ├── test_run_record.py
    └── test_github_projects_v2_provider.py
```

## File responsibilities

### Top-level docs and config

- `README.md`
  - User-facing usage and CLI examples.
  - Best first read for operating the tool.

- `DESIGN.md`
  - Architecture, state model, storage model, and intended behavior.
  - Source of truth for high-level behavior and invariants.

- `AGENTS.md`
  - Agent workflow rules for contributors/LLMs in this repo.

- `Makefile`
  - Convenience command(s), currently `make test`.

- `docs/specs/active/*.md`
  - Implementation specs and execution plans.
  - Useful for intent/history; one spec is still marked in progress (`2026-02-09-manage-inner-loop-state-machine.md`).

### `loops/` package

- `loops/__main__.py`
  - Entry-point shim for `python -m loops`.
  - Normalizes legacy argv into subcommand form.

- `loops/cli.py`
  - Click-based command surface (`init`, `run`, `inner-loop`, `doctor`, `clean`).
  - Builds default config payload on init.
  - Wires CLI options to outer/inner loop functions.

- `loops/outer_loop.py`
  - Outer polling/dispatch orchestration.
  - Config models (`OuterLoopConfig`, `LoopsConfig`, `InnerLoopCommandConfig`).
  - Outer state ledger (`outer_state.json`) read/write.
  - Run directory creation and inner-loop process launching.

- `loops/inner_loop.py`
  - Inner-loop state machine and Codex turn orchestration.
  - Reads/writes `run.json` as authoritative state.
  - Handles user handoff, polls PR status, runs cleanup, and exits on DONE.
  - Writes orchestration logs to `run.log` and streams agent output to `agent.log`.

- `loops/run_record.py`
  - Core dataclasses (`Task`, `RunPR`, `CodexSession`, `RunRecord`).
  - `derive_run_state` logic.
  - Validated persistence helpers (`read_run_record`, `write_run_record`).

- `loops/task_provider.py`
  - Provider protocol abstraction (`poll`).

- `loops/providers/github_projects_v2.py`
  - GitHub Projects V2 provider implementation.
  - Parses project URL, runs GraphQL via `gh`, maps project items to `Task`.

### Tests

- `tests/test_cli.py`
  - CLI behavior and init defaults.

- `tests/test_outer_loop.py`
  - Outer-loop scheduling, dedupe, config parsing, launcher behavior.

- `tests/test_inner_loop.py`
  - Inner-loop lifecycle/state transitions, review feedback loops, logging behavior.

- `tests/test_run_record.py`
  - Run record schema/state derivation and payload validation.

- `tests/test_github_projects_v2_provider.py`
  - URL parsing, GraphQL mapping/pagination, provider behavior.

## Runtime/generated files (not source of implementation)

These are generated under `.loops/` at runtime:

- `.loops/config.json` - runtime config used by outer loop.
- `.loops/outer_state.json` - dedupe ledger for polled tasks.
- `.loops/oloops.log` - outer-loop logs.
- `.loops/jobs/<run>/run.json` - per-run authoritative state.
- `.loops/jobs/<run>/run.log` - inner-loop orchestration log.
- `.loops/jobs/<run>/agent.log` - streamed Codex output.

## Where to change what

- Add or modify CLI flags/commands:
  - `loops/cli.py`
  - Possibly `loops/__main__.py` (for argv normalization changes)

- Change outer-loop polling/dispatch/dedupe behavior:
  - `loops/outer_loop.py`
  - Tests: `tests/test_outer_loop.py`

- Change inner-loop state transitions, retry behavior, prompt construction, or logs:
  - `loops/inner_loop.py`
  - Tests: `tests/test_inner_loop.py`

- Change run schema or state-derivation rules:
  - `loops/run_record.py`
  - Tests: `tests/test_run_record.py`
  - Update `DESIGN.md` if semantics changed.

- Add a new task provider:
  - New provider file under `loops/providers/`
  - Provider construction path in `loops/outer_loop.py` (`build_provider`)
  - Tests alongside provider tests

- Change GitHub Projects V2 mapping/query behavior:
  - `loops/providers/github_projects_v2.py`
  - Tests: `tests/test_github_projects_v2_provider.py`

## Recommended read order for an LLM

1. `README.md` (operational context)
2. `DESIGN.md` (architecture and invariants)
3. `loops/cli.py` and `loops/__main__.py` (entrypoints)
4. `loops/outer_loop.py` and `loops/inner_loop.py` (runtime logic)
5. `loops/run_record.py` (state contract)
6. Relevant test file(s) for the area being edited
