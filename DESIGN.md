# Loops design doc

## 0. Context
Loops is a lightweight LLM harness around coding agents. It has an outer loop that finds ready tasks and an inner loop that executes each task to completion.

## 1. Problem and scope

### Problem
Close the loop on building a coding agent harness that can pick up tasks, execute them, and handle review feedback until done.

### Goals
- Drive tasks from a provider into a coding agent with minimal human input.
- Persist run state so the system can resume after crashes or reboots.
- Keep the system extensible: new task providers and new agents should be pluggable.

### In scope
- Outer loop for polling tasks and starting inner loops.
- Inner loop for running Codex, tracking state, and handling PR review cycles.
- Local persistence under `.loops/`.

### Out of scope
- Full task management UI or assignment workflow.
- Non-Codex LLMs in the MVP.
- Automated merge/deploy; loops stops after cleanup.
- Complex scheduling, task dependencies, or cross-repo orchestration.

## 2. Constants and types

### Constants
- `LOOPS_ROOT = [REPO_ROOT]/.loops/`
- `INNER_LOOP_ROOT = [REPO_ROOT]/.loops/[yyyy-mm-dd]-[task_title_kebab_case]-[task_id]`

### Types
Source of truth: `/Users/kevinlin/kevin-garden/kevin-private/assets/loops-types.ts`.

Key types:
- `OuterLoopConfig`: poll interval, parallelism, task status filter, force mode.
- `InnerLoopConfig`: prompt, required skills, user handoff handler.
- `Task`: provider metadata (id, title, status, url, timestamps, optional repo).
- `TaskProvider`: provider interface with `poll()`.
- `TaskQueue`: provider aggregator with `poll()`.

## 3. Architecture

### High-level diagram

```
Task providers (GitHub Projects V2, etc.)
        |
        v
    TaskQueue  <----- outer loop config
        |
        v
    Outer loop  -------------------+------------------+
        |                           |                  |
        v                           v                  v
  Inner loop (task A)         Inner loop (task B)  Inner loop (task C)
        |
        v
   Codex CLI session  <-> PR review cycle  <-> user handoff
```

### Key components

#### Outer loop
- Initializes `TaskQueue` with the configured providers.
- Polls according to `poll_interval_seconds`.
- Filters tasks by `task_ready_status` and deduplicates using `(provider_id, id)`.
- Starts an inner loop per task (serially or in parallel based on config).
- Persists a minimal outer loop ledger to avoid re-processing completed tasks.

#### Inner loop
- Runs a state machine per task run.
- Uses `codex exec` to execute prompts and records a session id for resuming.
- Writes state transitions to `[INNER_LOOP_ROOT]/state_ledger.jsonl`.
- Handles PR feedback and user handoff.

#### Task providers
- Implement `TaskProvider.poll(limit)` for each provider.
- MVP: GitHub Projects V2 via the GitHub API or `gh`.

#### User handoff handler
- Called when the agent needs input.
- Default implementation reads from stdin and returns the user response.

## 4. Storage layout

`LOOPS_ROOT` is runtime state and logs. Each inner loop run gets its own directory.

```
.loops/
  oloops.log
  outer_state.json
  2026-02-02-fix-cache-12345/
    task.json
    config.json
    state_ledger.jsonl
    iloops.log
    codex_session.json
    pr.json
```

File responsibilities:
- `outer_state.json`: last poll timestamps and task ids already started.
- `task.json`: serialized `Task` from the provider.
- `config.json`: resolved `InnerLoopConfig` and run metadata.
- `state_ledger.jsonl`: append-only state transitions with timestamps.
- `codex_session.json`: session id and last prompt sent.
- `pr.json`: PR url/number/repo for review polling.

## 5. Control flow

### Outer loop algorithm
1. Load config and initialize providers.
2. Poll providers via `TaskQueue.poll(limit)`.
3. Filter tasks by `task_ready_status` and ignore any task already in `outer_state.json` unless `force=true`.
4. For each task:
   - Create `INNER_LOOP_ROOT` and write `task.json`.
   - Start the inner loop process with `InnerLoopConfig`.
5. Sleep for `poll_interval_seconds` and repeat.

### Inner loop state machine

States (append-only to `state_ledger.jsonl`):
- `GET_TASK`: load task and config.
- `PLAN_TASK`: Codex plans work (via `dev.do`).
- `EXECUTE_TASK`: Codex implements and opens a PR.
- `PUSHING_PR`: PR creation in progress.
- `WAITING_ON_PR_FEEDBACK`: PR exists, waiting for review/comments.
- `ADDRESSING_PR_FEEDBACK`: Codex addressing requested changes.
- `PR_APPROVED`: PR approved.
- `CLEANUP`: Codex cleanup phase.
- `NEED_USER_INPUT`: waiting for user response.

State transitions:
- `GET_TASK -> PLAN_TASK -> EXECUTE_TASK` on start.
- `EXECUTE_TASK -> PUSHING_PR -> WAITING_ON_PR_FEEDBACK` after PR creation.
- `WAITING_ON_PR_FEEDBACK -> ADDRESSING_PR_FEEDBACK` when review comments appear.
- `ADDRESSING_PR_FEEDBACK -> WAITING_ON_PR_FEEDBACK` after new changes are pushed.
- `WAITING_ON_PR_FEEDBACK -> PR_APPROVED` when approvals meet repo rules.
- `PR_APPROVED -> CLEANUP` then exit.
- Any state -> `NEED_USER_INPUT` when Codex requests human input or a recoverable error occurs.

### Prompts
Default prompts:
- `DO_TASK_PROMPT`: `use dev.do skill to do the following task: [task]`
- `REVIEW_TASK_PROMPT`: `please address pr feedback`
- `CLEANUP_PROMPT`: `invoke:cleanup to cleanup`

Prompt usage:
- Start with `DO_TASK_PROMPT`.
- Resume with `REVIEW_TASK_PROMPT` on review feedback.
- Resume with `CLEANUP_PROMPT` once PR is approved.

## 6. PR review handling

- Codex records PR metadata in `pr.json` after creation.
- The inner loop polls PR status (review comments, approvals) on a fixed interval.
- When review comments appear, it resumes the previous Codex session and sets state to `ADDRESSING_PR_FEEDBACK`.
- When approval conditions are met, it sets state to `PR_APPROVED` and runs cleanup.

## 7. Error handling and recovery

- Non-fatal errors cause a transition to `NEED_USER_INPUT` with the error message.
- Fatal errors still write to `state_ledger.jsonl` and terminate the run.
- The outer loop can resume runs by re-invoking inner loops based on the last ledger entry.

## 8. Observability

### Logging
- Outer loop logs: `[LOOPS_ROOT]/oloops.log`.
- Inner loop logs: `[INNER_LOOP_ROOT]/iloops.log`.

### Metrics (optional)
- Task pickup latency, time-to-PR, time-in-review, retries.

## 9. Security and safety

- Use GitHub auth from `gh` or environment-provided tokens.
- Avoid logging secrets; redact tokens in logs.
- Constrain Codex execution to the repo working directory.

## 10. MVP

### Implementation
- Only Codex and GitHub Projects V2 in the initial release.
- Use `TaskProvider` abstractions to keep additional providers pluggable.
- Recreate the inner loop MVP in Python (reference: `/Users/kevinlin/code/skills/active/dev.watch/scripts/loops.sh`).

## Appendix

### Prior art
- Outer loop MVP: `/Users/kevinlin/code/skills/active/dev.watch/scripts/dev_watch.py`.
- Inner loop MVP: `/Users/kevinlin/code/skills/active/dev.watch/scripts/loops.sh`.
