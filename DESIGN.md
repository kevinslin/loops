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
- `RUN_FILE = [INNER_LOOP_ROOT]/run.json`
- `RUN_LOG = [INNER_LOOP_ROOT]/run.log`

### Types
```ts
type OuterLoopConfig = {
    // default 30
    poll_interval_seconds: number
    // default false
    parallel_tasks: boolean
    // default 5
    parallel_tasks_limit: number
    // should count tasks initially or wait
    emit_on_first_run: boolean
    // ignore state transitions. imply emit_on_first_run=true
    force: boolean
    // status of the task that is ready to be processed
    task_ready_status: string
}

/**
 * Ask user for input
 */
type UserHandoffHandler = (message: string) => Promise<string>

type InnerLoopConfig = {
    // single prompt for the full task lifecycle
    prompt: string
    // skills required to process the task - need to check for existence at startup
    skills: string[]
    userHandoffHandler: UserHandoffHandler
}

type Task = {
    // the id of the task provider
    provider_id: string
    // task id from the provider
    id: string
    title: string
    status: string
    url: string
    created_at: string
    updated_at: string
    repo?: string

}

type RunState = "RUNNING" | "WAITING_ON_REVIEW" | "NEEDS_INPUT" | "PR_APPROVED" | "DONE"

type RunPR = {
    url: string
    number?: number
    repo?: string
    // open | changes_requested | approved
    review_status?: "open" | "changes_requested" | "approved"
    merged_at?: string
    last_checked_at?: string
}

type CodexSession = {
    id: string
    last_prompt?: string
}

type RunRecord = {
    task: Task
    pr?: RunPR
    codex_session?: CodexSession
    needs_user_input: boolean
    last_state: RunState
    updated_at: string
}

// Providers
type TaskProvider = {
    // unique identifier. eg. github_projects_v2
    id: string
    loop_config: OuterLoopConfig
    // source specific config
    provider_config: any
    // get matching tasks
    poll(limit?: number): Promise<Task[]>
} 

type GithubProjectsV2TaskProviderConfig = {
    url: string
    status_field: "Status"
}
```


Key types:
- `OuterLoopConfig`: poll interval, parallelism, task status filter, force mode.
- `InnerLoopConfig`: single prompt, required skills, user handoff handler.
- `Task`: provider metadata (id, title, status, url, timestamps, optional repo).
- `TaskProvider`: provider interface with `poll()`.
- `RunState`: `RUNNING | WAITING_ON_REVIEW | NEEDS_INPUT | DONE`.
- `RunRecord`: persisted run metadata for `run.json`.

## 3. Architecture

### High-level diagram

```
Task provider (GitHub Projects V2)
        |
        v
    Outer loop
        |
        v
    Inner loop (per task)
        |
        v
   Codex CLI session  <-> PR review cycle  <-> user handoff
```

### Key components

#### Outer loop
- Initializes a single `TaskProvider` (MVP: GitHub Projects V2).
- Polls according to `poll_interval_seconds`.
- Filters tasks by `task_ready_status` and ignores tasks already started unless `force=true`.
- Starts an inner loop per task (serially or in parallel based on config).
- Persists a minimal outer loop ledger to avoid re-processing completed tasks.

#### Inner loop
- Runs a small state model derived from PR status plus a single flag (`needs_user_input`).
- Uses `codex exec` to execute the single prompt and records a session id for resuming.
- Writes and updates `[INNER_LOOP_ROOT]/run.json`.
- Streams output to `[INNER_LOOP_ROOT]/run.log`.

#### Task provider
- Implements `TaskProvider.poll(limit)`.
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
    run.json
    run.log
```

`run.json` fields (minimal set):
- `task`: serialized `Task` from the provider.
- `pr`: `{ url, number, repo, review_status, merged_at, last_checked_at }`.
- `pr.merged_at`: optional ISO timestamp when the PR was merged.
- `codex_session`: `{ id, last_prompt }`.
- `needs_user_input`: boolean flag (readers must validate this is a boolean and reject malformed values).
- `last_state`: cached derived state.
- `updated_at`: ISO timestamp.

`last_state` is derived from `pr.review_status` and `needs_user_input` and is stored as a cache for easy inspection.

`outer_state.json` fields (minimal set):
- `initialized`: boolean flag indicating whether the outer loop has completed at least one poll.
- `tasks`: map keyed by `{provider_id}:{task_id}`.
  - `task`: serialized `Task` snapshot for the most recent poll.
  - `first_seen_at`: ISO timestamp of the first time the task was observed.
  - `last_seen_at`: ISO timestamp of the most recent observation.
- `updated_at`: ISO timestamp when the outer state was last persisted.

The outer loop uses `outer_state.json` as a dedupe ledger to avoid re-processing tasks unless `force=true`.

## 5. Control flow

### Outer loop algorithm
1. Load config and initialize the single provider.
2. Poll provider via `TaskProvider.poll(limit)`.
3. Filter tasks by `task_ready_status` and ignore any task already in `outer_state.json` unless `force=true`.
4. For each task:
   - Create `INNER_LOOP_ROOT` and write `run.json`.
   - Start the inner loop process with `InnerLoopConfig`.
5. Sleep for `poll_interval_seconds` and repeat.

### Inner loop state model

Derived states:
- `NEEDS_INPUT` if `needs_user_input == true`.
- `DONE` if a PR exists and `merged_at` is set (merged).
- `PR_APPROVED` if a PR exists, `review_status` is approved, and `needs_user_input == false`.
- `WAITING_ON_REVIEW` if a PR exists and `review_status` is not approved.
- `RUNNING` otherwise.

State derivation uses only PR status plus the single `needs_user_input` flag; `last_state` is cached in `run.json`.

### Prompt
Single prompt used for initial run and all resumes:

```
Use dev.do to implement the task, open a PR, wait for review, address feedback, and cleanup when approved.
Task: [task]
```

## 6. PR review handling

- When a PR is opened, the inner loop records it in `run.json`.
- The inner loop polls PR status and updates `pr.review_status`.
- If review comments appear, the same Codex session is resumed with the same prompt.
- When approval is detected, the inner loop runs cleanup immediately; if cleanup fails it sets `needs_user_input=true`.

## 7. Error handling and recovery

- Non-fatal errors set `needs_user_input=true` and write the error message to `run.log`.
- Fatal errors still write to `run.json` and terminate the run.
- On restart, the inner loop recomputes derived state from `run.json` and resumes accordingly.

## 8. Observability

### Logging
- Outer loop logs: `[LOOPS_ROOT]/oloops.log`.
- Inner loop logs: `[INNER_LOOP_ROOT]/run.log`.

### Metrics (optional)
- Task pickup latency, time-to-PR, time-in-review, retries.

## 9. Security and safety

- Use GitHub auth from `gh` or environment-provided tokens.
- Avoid logging secrets; redact tokens in logs.
- Constrain Codex execution to the repo working directory.

## 10. MVP

### Implementation
- Only Codex and GitHub Projects V2 in the initial release.
- Single provider (no `TaskQueue`) with a thin wrapper added later if needed.
- Recreate the inner loop MVP in Python (reference: `/Users/kevinlin/code/skills/active/dev.watch/scripts/loops.sh`).

## Appendix

### Prior art
- Outer loop MVP: `/Users/kevinlin/code/skills/active/dev.watch/scripts/dev_watch.py`.
- Inner loop MVP: `/Users/kevinlin/code/skills/active/dev.watch/scripts/loops.sh`.
