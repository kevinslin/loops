# Agent Guidance

## Usage
- Always refer to DESIGN.md before starting work.
- Use the relevant active spec in `docs/specs/` when present. Completed specs live in `docs/specs/.archive/`.
- Keep project-level status in `progress.md`; keep spec-specific progress and learnings in `.agents/runs/`.
- If current changes deviate or add more detail to DESIGN.md, make sure to also edit DESIGN.md
- When updating `loop_config` schema/defaults, always update `loops doctor` (config upgrade path + tests) so older configs are backfilled correctly.
- When changing config schema/defaults (`loop_config` or provider config), bump config `version`, treat it as a breaking change (do not add runtime backward-compat paths), and add/update `loops doctor` migration logic + tests.
- When changing Loops config version/schema/defaults, also update `tests/integ/test_end2end_live.py` (`write_end2end_config`) and any related integration docs/tests that depend on generated config shape.
- When introducing changes - always ask if the design could be simpler. We want to keep loops as simple as possible.

## Parameters and Configuration
- `.loops/config.json`: outer loop configuration (provider + loop + inner loop command).
- `LOOPS_RUN_DIR`: path to the inner loop run directory (required for inner loop runner).
- `CODEX_CMD`: command used to invoke Codex (default: `codex exec --yolo`).
- `LOOPS_PROMPT_FILE` / `CODEX_PROMPT_FILE`: optional base prompt file path.
- `LOOPS_TASK_ID`, `LOOPS_TASK_TITLE`, `LOOPS_TASK_URL`, `LOOPS_TASK_PROVIDER`: task metadata injected by the outer loop launcher.

## Shortcuts

### add-test-task
In loops-integ github repo, creat a new issue.
Come up with a random animal and replace [animal] with it.

Title: Create [animal].md file 
Body: Contents of file is ascii art of [animal] saying a bad pun.

### add-test-task-with-bug

run trigger:add-test-task but change the body. have the [animal] say TODO and wait for pr comment to supply contents.

## Additional Notes
- this project does not use statsig. ignore all statsig related directives
- when making changes, use $dev.research to understand flow docs and search over all related flow docs. if the current change has changed the design of a flow doc - trigger:update-flow-doc to update it

<!-- ag-ledger:begin -->
## ag-ledger

Use `$ag-ledger` and the `ag-ledger` CLI for activity tracking.
Always log these moments:
- Session start
- Notable change
- Session end

Run:
- `ag-ledger append-current "session start: <plan>"`
- `ag-ledger append-current "notable change: <what changed>"`
- `ag-ledger append-current "session end: <outcome>"`
- `ag-ledger session-id` (prints `CODEX_THREAD_ID`)

Manual fallback (non-Codex or explicit session ids):
- `ag-ledger append <session-id> "session start: <plan>"`
- `ag-ledger append <session-id> "notable change: <what changed>"`
- `ag-ledger append <session-id> "session end: <outcome>"`

If `ag-ledger` is not on PATH, run:
- `/Users/kevinlin/code/skills/active/ag-ledger/scripts/ag-ledger`
<!-- ag-ledger:end -->
