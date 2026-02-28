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
- `INNER_LOOP_RUNS_ROOT = [REPO_ROOT]/.loops/jobs/`
- `INNER_LOOP_ROOT = [REPO_ROOT]/.loops/jobs/[yyyy-mm-dd]-[task_title_kebab_case]-[task_id]`
- `RUN_FILE = [INNER_LOOP_ROOT]/run.json`
- `RUN_LOG = [INNER_LOOP_ROOT]/run.log`
- `AGENT_LOG = [INNER_LOOP_ROOT]/agent.log`

### Types
```ts
type OuterLoopConfig = {
    // default 30
    poll_interval_seconds: number
    // default false
    parallel_tasks: boolean
    // default 5
    parallel_tasks_limit: number
    // default false. run inner loop in foreground (interactive).
    sync_mode: boolean
    // should count tasks initially or wait
    emit_on_first_run: boolean
    // ignore state transitions. imply emit_on_first_run=true
    force: boolean
    // status of the task that is ready to be processed
    task_ready_status: string
    // allowlisted usernames whose approval comments can mark PR approved
    approval_comment_usernames: string[]
    // regex pattern for approval comments from allowlisted usernames
    approval_comment_pattern: string
    // NEEDS_INPUT handoff strategy
    // stdin_handler | gh_comment_handler
    handoff_handler: string
}

/**
 * Ask user for input
 */
type HandoffResult = {
    // waiting means keep polling; response means continue with returned text
    status: "waiting" | "response"
    response?: string
}

type UserHandoffHandler = (payload: {
    message: string
    context?: Record<string, unknown>
}) => Promise<HandoffResult | string>

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
    // timestamp of latest GitHub review event matching the current reviewDecision
    latest_review_submitted_at?: string
    // timestamp of the review event last addressed by trigger:fix-pr
    review_addressed_at?: string
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
    // get matching tasks.
    // github_projects_v2 ordering: oldest task first by created_at, then limit
    poll(limit?: number): Promise<Task[]>
} 

type GithubProjectsV2TaskProviderConfig = {
    url: string
    status_field: "Status"
}

type SecretRequirement = {
    // environment variable name expected by Loops preflight checks
    name: string
    // optional alias env var names accepted as fallback
    alias?: string[]
    // short explanation shown when the variable is missing
    description: string
}

type LoopsProviderConfig = {
    // unique provider id (must match provider_id in .loops/config.json)
    id: string
    // optional display name; defaults to id when absent
    name?: string
    // required env vars for provider operation (validation only in MVP)
    required_secrets: SecretRequirement[]
    // provider-owned pydantic model for validating provider_config payload
    provider_config_model: "pydantic model"
}
```


Key types:
- `OuterLoopConfig`: poll interval, parallelism, task status filter, force mode.
- `InnerLoopConfig`: single prompt, required skills, user handoff handler.
- `Task`: provider metadata (id, title, status, url, timestamps, optional repo).
- `TaskProvider`: provider interface with `poll()`.
- `RunState`: `RUNNING | WAITING_ON_REVIEW | NEEDS_INPUT | PR_APPROVED | DONE`.
- `RunRecord`: persisted run metadata for `run.json`.

## 3. Configuration

### Config file
- Default path: `.loops/config.json`
- Top-level keys: `version`, `provider_id`, `provider_config`, `loop_config`, `inner_loop`
- `inner_loop` keys: `command`, `working_dir`, `env`, `append_task_url`

Config shape:
```ts
type LoopsConfigFile = {
    version: number
    provider_id: "github_projects_v2"
    provider_config: GithubProjectsV2TaskProviderConfig
    loop_config?: OuterLoopConfig
    inner_loop?: InnerLoopCommandConfig
}

type OuterLoopConfig = {
    poll_interval_seconds?: number
    parallel_tasks?: boolean
    parallel_tasks_limit?: number
    sync_mode?: boolean
    emit_on_first_run?: boolean
    force?: boolean
    task_ready_status?: string
    approval_comment_usernames?: string[]
    approval_comment_pattern?: string
    handoff_handler?: string
}

type InnerLoopCommandConfig = {
    command: string | string[]
    working_dir?: string
    env?: Record<string, string>
    append_task_url?: boolean
}

type GithubProjectsV2TaskProviderConfig = {
    url: string
    status_field: "Status"
    page_size?: number
    github_token?: string
    // provider-side key=value filters. supported keys:
    // repository=<owner>/<repo> (OR across repository entries)
    // tag=<label-name> (AND across tag entries)
    filters?: string[]
}
```

Notes:
- `version` tracks config schema revisions; legacy configs without `version` are treated as version `0`.
- `loops doctor` upgrades `config.json` to the latest supported version and fills missing `loop_config` keys with defaults.
- `provider_id` currently supports only `"github_projects_v2"`.
- `provider_config` is validated by the provider's Pydantic model.
- `provider_config.filters` supports provider-side `key=value` filters for GitHub Projects V2 (`repository`, `tag`).
- Required provider secrets are validated from environment variables before provider construction.
- `loop_config` is optional; omitted keys fall back to defaults.
- `loop_config.approval_comment_usernames` allows comment-based PR approval overrides from specific usernames.
- `loop_config.approval_comment_pattern` controls which comment bodies count as approval signals.
- `loop_config.handoff_handler` selects built-in NEEDS_INPUT handoff behavior (`stdin_handler` default, `gh_comment_handler` for issue-comment handoff).
- `inner_loop` is optional when running via the CLI; if omitted, the CLI uses
  `python -m loops.inner_loop` with `append_task_url=false`.
- `python -m loops run --task-url <task-url>` targets exactly one task from the provider poll, implies `run-once`, `force=true`, and `sync_mode=true`, and does not mutate `provider_config.url`.
- Installed package entrypoint `loops` is equivalent to `python -m loops` and uses the same argv normalization.

### Environment variables
- `GITHUB_TOKEN` or `GH_TOKEN`: required for GitHub provider startup checks (`GH_TOKEN` is supported as alias fallback).
- `LOOPS_RUN_DIR`: required path to the inner loop run directory.
- `CODEX_CMD`: command used to invoke Codex (default: `codex exec --yolo`).
- `LOOPS_PROMPT_FILE` / `CODEX_PROMPT_FILE`: optional base prompt file path.
- `LOOPS_TASK_ID`, `LOOPS_TASK_TITLE`, `LOOPS_TASK_URL`, `LOOPS_TASK_PROVIDER`: task metadata injected by the outer loop launcher.
- `LOOPS_HANDOFF_HANDLER`: selected built-in handoff handler injected by outer loop from `loop_config.handoff_handler`.
- `LOOPS_STREAM_LOGS_STDOUT`: set to `1` by the outer loop when `sync_mode=true` so inner-loop log writes are also mirrored to stdout.

## 4. Architecture

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
- Starts an inner loop per task (detached by default; foreground when `sync_mode=true`).
- Persists a minimal outer loop ledger to avoid re-processing completed tasks.
- In `sync_mode=true`, mirrors outer-loop log lines to stdout in addition to `oloops.log`.

#### Inner loop
- Runs a small state model derived from PR status plus a single flag (`needs_user_input`).
- Uses `codex exec` for the first turn, then `codex exec resume <session_id>` for subsequent turns when `codex_session.id` is available.
- Is the single writer for `[INNER_LOOP_ROOT]/run.json`.
- Consumes model-authored signals from a run-local queue and applies validated state changes to `run.json`.
- Writes inner-loop orchestration logs to `[INNER_LOOP_ROOT]/run.log` and appends Codex output there.
- Streams Codex/agent output to `[INNER_LOOP_ROOT]/agent.log`.
- In `sync_mode=true`, also mirrors inner-loop `run.log` lines to stdout.
- Supports a manual `--reset` operation to clear orchestration/session/input fields in `run.json` while preserving task metadata and existing PR link identity.

#### Task provider
- Implements `TaskProvider.poll(limit)`.
- MVP: GitHub Projects V2 via the GitHub API or `gh`.
- Each provider declares `LoopsProviderConfig` metadata for identity, required env secrets, and typed `provider_config` validation via a provider-owned Pydantic model.

#### Signal CLI (state requests)
- Purpose: allow the model to request a state transition (MVP: `NEEDS_INPUT`) without writing `run.json` directly.
- Interface: append-only queue write to `[INNER_LOOP_ROOT]/state_signals.jsonl`.
- Ownership: this CLI writes only the signal queue; it does not mutate `run.json`.
- Current status: not implemented in code yet (`loops/state_signal.py` is planned).

#### User handoff handler
- Called when the agent needs input.
- Default implementation reads from stdin and returns the user response.

## 5. Storage layout

`LOOPS_ROOT` is runtime state and logs. Each inner loop run gets its own directory.

```
.loops/
  oloops.log
  outer_state.json
  jobs/
    2026-02-02-fix-cache-12345/
      run.json
      run.log
      agent.log
      state_signals.jsonl
```

`run.json` fields (minimal set):
- `task`: serialized `Task` from the provider.
- `pr`: `{ url, number, repo, review_status, merged_at, last_checked_at }`.
- `pr.merged_at`: optional ISO timestamp when the PR was merged.
- `codex_session`: `{ id, last_prompt }`.
- `needs_user_input`: boolean flag (readers must validate this is a boolean and reject malformed values).
- `needs_user_input_payload`: optional JSON object used to carry handoff context (for example `{ "message": "...", "context": {...} }`).
- `last_state`: cached derived state.
- `updated_at`: ISO timestamp.

`last_state` is derived from `pr.review_status` and `needs_user_input` and is stored as a cache for easy inspection.

`state_signals.jsonl`:
- Append-only queue of state intents written by model tooling.
- MVP signal type: `NEEDS_INPUT` only.
- Each line is a JSON object with at least `state` and `args`.
- The inner loop validates and applies each queued signal; malformed entries are rejected and logged.

`outer_state.json` fields (minimal set):
- `initialized`: boolean flag indicating whether the outer loop has completed at least one poll.
- `tasks`: map keyed by `{provider_id}:{task_id}`.
  - `task`: serialized `Task` snapshot for the most recent poll.
  - `first_seen_at`: ISO timestamp of the first time the task was observed.
  - `last_seen_at`: ISO timestamp of the most recent observation.
- `updated_at`: ISO timestamp when the outer state was last persisted.

The outer loop uses `outer_state.json` as a dedupe ledger to avoid re-processing tasks unless `force=true`.

## 6. Control flow

### Outer loop algorithm
1. Load config and initialize the single provider.
2. Poll provider via `TaskProvider.poll(limit)`.
3. Filter tasks by `task_ready_status` and ignore any task already in `outer_state.json` unless `force=true`.
4. For each task:
   - Create `INNER_LOOP_ROOT` and write `run.json`.
   - Start the inner loop process with `InnerLoopConfig`.
5. Sleep for `poll_interval_seconds` and repeat.

### Inner loop state model

#### Prerequisites
- **Signals skill**: The LLM uses a skill (e.g. `$needs_input`) to send signals to the inner loop via the signal queue (`state_signals.jsonl`).
- **State file (S)**: `run.json` is the persisted state file. `last_state` caches the derived state. The state file is the single source of truth.
- **Retry**: When the inner loop starts and finds an existing state file, it is resuming after a crash. Each state defines its own retry behavior for idempotent recovery.

#### States

| State | Description |
|-------|-------------|
| `RUNNING` | LLM executing the task with the current prompt context. |
| `NEEDS_INPUT` | LLM needs human input before continuing. |
| `WAITING_ON_REVIEW` | PR submitted. Polling for reviewer feedback. |
| `PR_APPROVED` | PR approved. Running merge and post-merge cleanup. |
| `DONE` | PR merged. Terminal state. |

#### State derivation
- `NEEDS_INPUT` if `needs_user_input == true`.
- `DONE` if a PR exists and `merged_at` is set (merged).
- `PR_APPROVED` if a PR exists, `review_status` is approved, and `needs_user_input == false`.
- `WAITING_ON_REVIEW` if a PR exists and `review_status` is not approved.
- `RUNNING` otherwise.

State derivation uses only PR status plus the single `needs_user_input` flag; `last_state` is cached in `run.json`.

Precedence rule: `NEEDS_INPUT` has priority over `DONE`; if `needs_user_input=true`, state is `NEEDS_INPUT` even when `pr.merged_at` is set.

#### Logic

**Initial entry (no state file):**
- LLM: start with prompt tagged `<state>RUNNING</state>`.
- Retry: resume the prior session ID and send the next state-tagged prompt through `codex exec resume <session_id>` when a prior session is recorded.

**Loop** (read `run.json`, derive state, dispatch):

- **If `NEEDS_INPUT`**: send signal S:NEEDS_INPUT, payload: `{ questions }`. Block until user responds. Clear flag, persist answer, resume LLM. Retry: still wait for input.
- **If PR submitted → `WAITING_ON_REVIEW`**: set S:WAITING_ON_REVIEW. Run poll script. If changes requested AND `latest_review_submitted_at > review_addressed_at` (new review event), exec trigger:fix-pr and record `review_addressed_at`. If approved, transition to PR_APPROVED. Retry: continue polling.
- **If PR approved → `PR_APPROVED`**: set S:PR_APPROVED. Run trigger:merge-pr. On success, derive DONE from `pr.merged_at`. Retry: re-run trigger (idempotent).
- **Bounded wait guardrail (review + approved states)**: both `WAITING_ON_REVIEW` and `PR_APPROVED` use idle-poll escalation. If status polling fails repeatedly or the state does not progress for `max_idle_polls` consecutive polls (default `20`), force `NEEDS_INPUT` with a manual-guidance payload. Poll backoff grows from `initial_poll_seconds` (default `5s`) up to `max_poll_seconds` (default `60s`).

### State transitions (ASCII)

```text
                         (no state file)
                               |
                               v
                        +--------------+
                        |   RUNNING    |  (LLM executing task)
                        +--------------+
                          |          |
          PR submitted    |          | needs input
                          v          v
                 +------------------------+    +---------------+
                 |   WAITING_ON_REVIEW    |    |  NEEDS_INPUT  |
                 +------------------------+    +---------------+
                    |           |                |
       changes      |           | approved       | user responds
       requested    |           |                |
          |         |           v                v
          v         |     +------------------+   (back to RUNNING
  trigger:fix-pr    |     |   PR_APPROVED    |    or WAITING_ON_REVIEW)
  (back to          |     +------------------+
   WAITING_ON_REVIEW)     |           |
                    |           | trigger:merge-pr
                    |           | pr.merged_at set
                    |           v
                    |     +-------------+
                    +---->|    DONE     |
                          +-------------+

From any non-DONE state:
  needs_user_input = true  ->  NEEDS_INPUT
```

### Retry behavior

| State | On crash / restart | Action |
|-------|--------------------|--------|
| `RUNNING` (no state file) | State file missing | Start fresh with prompt |
| `RUNNING` (session recorded) | Resume existing session | Resume session ID and send the next state-tagged prompt |
| `NEEDS_INPUT` | Still waiting | Re-enter wait; do not re-send signal |
| `WAITING_ON_REVIEW` | Polling interrupted | Continue polling PR status |
| `PR_APPROVED` | Merge may be partial | Re-run trigger:merge-pr (idempotent); if merge remains stalled past idle threshold, escalate to `NEEDS_INPUT` |
| `DONE` | Terminal | Exit immediately |

### Signal handling

- `run.json` is authoritative for lifecycle state.
- Model output text is not authoritative for state transitions.
- Model tools request state changes through the signals skill, which appends to `state_signals.jsonl`.
- Inner loop consumes queued signals in-order and is the only process that persists resulting state to `run.json`.
- For `NEEDS_INPUT`, the inner loop sets `needs_user_input=true`, persists `needs_user_input_payload`, and blocks on user handoff until a response is available.
- After handoff completes, inner loop clears `needs_user_input` and `needs_user_input_payload`, writes `run.json`, and resumes the state machine.

### Triggers

| Trigger | Invoked from | Description |
|---------|-------------|-------------|
| `trigger:fix-pr` | `WAITING_ON_REVIEW` | Resume Codex to address review feedback and update the PR. |
| `trigger:merge-pr` | `PR_APPROVED` | Merge the PR and run post-merge cleanup. Idempotent. |

### CLI callers

- `python -m loops` is the top-level wrapper CLI.
- `python -m loops init` initializes `.loops/` scaffolding (`jobs/`, logs, state, default config).
- `python -m loops doctor` upgrades config schema/default values in `config.json`.
- `python -m loops run` starts the outer loop runner.
- `python -m loops inner-loop` runs one inner-loop execution for a run directory.
- `python -m loops signal` enqueues a run-local signal (MVP: `NEEDS_INPUT`).
- Direct module callers still work (`python -m loops.inner_loop`, `python -m loops.state_signal`).

### Prompt
Single prompt template used for initial run and resume turns:

```text
Use dev.do to implement the task, open a PR, wait for review, address feedback, and cleanup when approved.
If needing input from user, use "$needs_input" skill to request user input.
The current inner-loop state is passed via a trailing <state>...</state> tag; initial state is <state>RUNNING</state>.
Do not merge until the state is exactly <state>PR_APPROVED</state>.
Task: [task]
<state>RUNNING</state>
```

## 7. PR review handling

- When a PR is opened, the inner loop records it in `run.json`.
- The inner loop polls PR status and updates `pr.review_status`.
- When a review requests changes, the inner loop records `latest_review_submitted_at` (the review's `submittedAt` timestamp from GitHub) and invokes Codex to address the feedback. After Codex runs, `review_addressed_at` is set to `latest_review_submitted_at`. On subsequent polls, the loop only re-invokes Codex if `latest_review_submitted_at > review_addressed_at`, indicating a genuinely new review event. This prevents duplicate fix attempts when the reviewer has not yet re-reviewed.
- When approval is detected (GitHub review decision or allowlisted approval comment newer than latest `CHANGES_REQUESTED` review), the inner loop runs cleanup immediately and appends `<state>PR_APPROVED</state>` to that cleanup prompt; if cleanup fails it sets `needs_user_input=true`.

## 8. Error handling and recovery

- Non-fatal errors set `needs_user_input=true` and write the error message to `run.log`.
- Fatal errors still write to `run.json` and terminate the run.
- On restart, the inner loop recomputes derived state from `run.json` and resumes accordingly.
- Repeated polling idleness in `WAITING_ON_REVIEW` or `PR_APPROVED` forces `NEEDS_INPUT` after the configured idle threshold.

## 9. Observability

### Logging
- Outer loop logs: `[LOOPS_ROOT]/oloops.log`.
- Outer loop per-task scheduling entries include the created inner-loop run directory path.
- Inner loop orchestration logs + Codex output mirror: `[INNER_LOOP_ROOT]/run.log`.
- Agent/Codex logs: `[INNER_LOOP_ROOT]/agent.log`.
- In `sync_mode=true`, outer-loop and inner-loop log lines are mirrored to stdout while still being persisted to files.

### Metrics (optional)
- Task pickup latency, time-to-PR, time-in-review, retries.

## 10. Security and safety

- Use GitHub auth from `gh` or environment-provided tokens.
- Avoid logging secrets; redact tokens in logs.
- Constrain Codex execution to the repo working directory.

## 11. MVP

### Implementation
- Only Codex and GitHub Projects V2 in the initial release.
- Single provider (no `TaskQueue`) with a thin wrapper added later if needed.
- Recreate the inner loop MVP in Python (reference: `/Users/kevinlin/code/skills/active/dev.watch/scripts/loops.sh`).

## Appendix

### Prior art
- Outer loop MVP: `/Users/kevinlin/code/skills/active/dev.watch/scripts/dev_watch.py`.
- Inner loop MVP: `/Users/kevinlin/code/skills/active/dev.watch/scripts/loops.sh`.
