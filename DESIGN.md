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

## 1.5 Core concepts

- `Task`: Normalized provider work item that Loops can process.
- `TaskProvider`: Provider adapter that returns normalized tasks via `poll(limit?)`.
- `OuterLoop`: Poll-and-dispatch runtime that discovers ready tasks and schedules runs.
- `Run`: One lifecycle execution context for one task, materialized as one run directory.
- `InnerLoop`: State machine for a single run from first Codex turn to terminal completion.
- `Turn`: One Codex subprocess invocation/return cycle inside the inner loop.
- `RunRecord`: Persisted lifecycle snapshot in `run.json`.
- `RunState`: Derived lifecycle state: `RUNNING | WAITING_ON_REVIEW | NEEDS_INPUT | PR_APPROVED | DONE`.
- `OuterState`: Outer-loop dedupe ledger persisted in `.loops/outer_state.json`.
- `Handoff`: `NEEDS_INPUT` user-input collection path (`stdin_handler` or `gh_comment_handler`).
- `Trigger`: Named inner-loop transition action such as `trigger:fix-pr` and `trigger:merge-pr`.

Type/interface mapping (section 2 and runtime):
- `Task` concept -> `Task` type.
- `TaskProvider` concept -> `TaskProvider` interface (`poll(limit?) -> Promise<Task[]>`).
- `RunRecord` concept -> `RunRecord` type and `run.json`.
- `RunState` concept -> `RunState` type.
- `OuterState` concept -> `.loops/outer_state.json` ledger (`OuterLoopState` runtime model).
- `Handoff` concept -> `UserHandoffHandler`, `HandoffResult`, and `loop_config.handoff_handler`.

## 2. Constants and types

### Constants
- `LOOPS_ROOT = [REPO_ROOT]/.loops/`
- `ARCHIVE_ROOT = [REPO_ROOT]/.loops/.archive/`
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
    // allowlisted usernames whose approval signals can mark PR approved
    approval_comment_usernames: string[]
    // regex pattern for approval text in comments/reviews from allowlisted usernames
    approval_comment_pattern: string
    // run ag-judge before merge when review and CI gates are satisfied
    auto_approve_enabled: boolean
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

type RunState =
    | "RUNNING"
    | "WAITING_ON_REVIEW"
    | "NEEDS_INPUT"
    | "PR_APPROVED"
    | "DONE"

type CIStatus = "pending" | "success" | "failure" | "unknown"
type AutoApproveVerdict = "none" | "APPROVE" | "REJECT" | "ESCALATE"

type RunPR = {
    url: string
    number?: number
    repo?: string
    // open | changes_requested | approved
    review_status?: "open" | "changes_requested" | "approved"
    // pending | success | failure | unknown
    ci_status?: CIStatus
    ci_last_checked_at?: string
    merged_at?: string
    last_checked_at?: string
    // timestamp of latest GitHub review event matching the current reviewDecision
    latest_review_submitted_at?: string
    // timestamp of the review event last addressed by trigger:fix-pr
    review_addressed_at?: string
}

type RunAutoApprove = {
    verdict?: AutoApproveVerdict
    impact?: 1 | 2 | 3 | 4 | 5
    risk?: 1 | 2 | 3 | 4 | 5
    size?: 1 | 2 | 3 | 4 | 5
    judged_at?: string
    summary?: string
}

type CodexSession = {
    id: string
    last_prompt?: string
}

type RunRecord = {
    task: Task
    pr?: RunPR
    auto_approve?: RunAutoApprove
    codex_session?: CodexSession
    needs_user_input: boolean
    // effective inner-loop run.log stdout mirroring setting for this run
    stream_logs_stdout?: boolean
    last_state: RunState
    updated_at: string
}

// Providers
type TaskProvider = {
    // unique identifier. eg. github_projects_v2
    id: string
    loop_config: OuterLoopConfig
    // source specific config
    task_provider_config: any
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
    // unique provider id (must match task_provider_id in .loops/config.json)
    id: string
    // optional display name; defaults to id when absent
    name?: string
    // required env vars for provider operation (validation only in MVP)
    required_secrets: SecretRequirement[]
    // provider-owned pydantic model for validating task_provider_config payload
    provider_config_model: "pydantic model"
}
```


Key types:
- `OuterLoopConfig`: poll interval, parallelism, task status filter, force mode, and merge-gate controls (CI + auto-approve evaluation).
- `InnerLoopConfig`: single prompt, required skills, user handoff handler.
- `Task`: provider metadata (id, title, status, url, timestamps, optional repo).
- `TaskProvider`: provider interface with `poll(limit?)`.
- `RunState`: `RUNNING | WAITING_ON_REVIEW | NEEDS_INPUT | PR_APPROVED | DONE`.
- `RunRecord`: persisted run metadata for `run.json`.

## 3. Configuration

### Config file
- Default path: `.loops/config.json`
- Top-level keys: `version`, `task_provider_id`, `task_provider_config`, `loop_config`, `inner_loop`
- `inner_loop` keys: `command`, `working_dir`, `env`, `append_task_url`

Config shape:
```ts
type LoopsConfigFile = {
    version: number
    task_provider_id: "github_projects_v2"
    task_provider_config: GithubProjectsV2TaskProviderConfig
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
    auto_approve_enabled?: boolean
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
    // usernames allowed to contribute review-phase PR comments/review signals
    allowlist?: string[]
    // provider-side key=value filters. supported keys:
    // repository=<owner>/<repo> (OR across repository entries)
    // tag=<label-name> (AND across tag entries)
    filters?: string[]
}
```

Notes:
- `version` tracks config schema revisions; legacy configs without `version` are treated as version `0`.
- `loops doctor` upgrades `config.json` to the latest supported version and fills missing `loop_config` keys with defaults.
- `task_provider_id` currently supports only `"github_projects_v2"`.
- `task_provider_config` is validated by the provider's Pydantic model.
- `task_provider_config` init defaults are emitted from provider-owned canonical builders (for `github_projects_v2`, from `loops.providers.github_projects_v2`).
- `loops doctor` also backfills missing GitHub `task_provider_config` defaults (`status_field`, `page_size`, `allowlist`) without overwriting existing values.
- `task_provider_config.filters` supports provider-side `key=value` filters for GitHub Projects V2 (`repository`, `tag`).
- `task_provider_config.allowlist` (GitHub provider) restricts review-phase PR comment/review signals to listed usernames; non-allowlisted actors are ignored during review polling.
- Required provider secrets are validated from environment variables before provider construction.
- `loop_config` is optional; omitted keys fall back to defaults.
- `loop_config` defaults are sourced from one canonical implementation in `loops.outer_loop` and reused by `loops init`, `loops doctor`, and runtime config loading.
- `loop_config.approval_comment_usernames` allows comment-based PR approval overrides from specific usernames.
- `loop_config.approval_comment_pattern` controls which comment bodies count as approval signals.
- `loop_config.auto_approve_enabled` enables the additional auto-approve path while the PR is still not approved.
- Auto-approve defaults are fixed when enabled: CI green is required and `ag-judge` uses `references/jb.coding.md`.
- `loop_config.handoff_handler` selects built-in NEEDS_INPUT handoff behavior (`stdin_handler` default, `gh_comment_handler` for issue-comment handoff).
- `inner_loop` is optional when running via the CLI; if omitted, the CLI uses
  a canonical default builder for `python -m loops.inner_loop` with `append_task_url=false`.
- `python -m loops run --task-url <task-url>` targets exactly one task from the provider poll, implies `run-once`, `force=true`, and `sync_mode=true`, and does not mutate `task_provider_config.url`.
- Installed package entrypoint `loops` is equivalent to `python -m loops` and uses the same argv normalization.

### Environment variables
- `GITHUB_TOKEN` or `GH_TOKEN`: required for GitHub provider startup checks (`GH_TOKEN` is supported as alias fallback).
- `LOOPS_RUN_DIR`: required path to the inner loop run directory.
- `CODEX_CMD`: fallback command used when run-scoped runtime config does not set one (default: `codex exec --yolo`).
- `LOOPS_PROMPT_FILE` / `CODEX_PROMPT_FILE`: fallback base prompt file path when run-scoped runtime config does not set one.
- `LOOPS_HANDOFF_HANDLER`: direct/manual-run fallback built-in handoff handler name.
- `LOOPS_TASK_ID`, `LOOPS_TASK_TITLE`, `LOOPS_TASK_URL`, `LOOPS_TASK_PROVIDER`: legacy fallback task metadata used only when resetting a run with missing `run.json`.
- `LOOPS_STREAM_LOGS_STDOUT`: direct/manual-run fallback toggle for mirroring `run.log` lines to stdout.
- Outer-loop-launched runs persist runtime settings in `inner_loop_runtime_config.json` under each run directory, instead of injecting config via child-process env vars. For non-`loops.inner_loop` custom launch commands, `inner_loop.env` remains merged into child env for backward compatibility.
- When `inner_loop_runtime_config.json` exists but is malformed, inner loop startup fails fast instead of silently falling back to process environment defaults.

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
- Writes inner-loop orchestration logs to `[INNER_LOOP_ROOT]/run.log` and appends Codex output there.
- Streams Codex/agent output to `[INNER_LOOP_ROOT]/agent.log`.
- In `sync_mode=true`, also mirrors inner-loop `run.log` lines to stdout.
- Supports a manual `--reset` operation to clear orchestration/session/input fields in `run.json` while preserving task metadata and existing PR link identity.

#### Task provider
- Implements `TaskProvider.poll(limit)`.
- MVP: GitHub Projects V2 via the GitHub API or `gh`.
- Each provider declares `LoopsProviderConfig` metadata for identity, required env secrets, and typed `task_provider_config` validation via a provider-owned Pydantic model.

#### Clean CLI (run artifact janitor)
- Purpose: clean stale run artifacts under `LOOPS_ROOT`.
- Interface: scans `jobs/` run directories and classifies actions:
  - delete run dir when both `run.log` and `agent.log` exist and are empty and the run is not in an active state.
  - archive run dir when `run.json.last_state == "DONE"` (unless already classified for deletion).
- Archive target: move completed runs to `ARCHIVE_ROOT`, adding numeric suffixes on name collisions (`-1`, `-2`, ...).
- `--dry-run`: reports planned delete/archive actions without mutating filesystem state.

#### User handoff handler
- Called when the agent needs input.
- Default implementation reads from stdin and returns the user response.

## 5. Storage layout

`LOOPS_ROOT` is runtime state and logs. Each inner loop run gets its own directory.

```
.loops/
  .archive/
    2026-02-02-fix-cache-12345/
      run.json
      run.log
      agent.log
  oloops.log
  outer_state.json
  jobs/
    2026-02-02-fix-cache-12345/
      run.json
      run.log
      agent.log
```

`run.json` fields (minimal set):
- `task`: serialized `Task` from the provider.
- `pr`: `{ url, number, repo, review_status, ci_status, ci_last_checked_at, merged_at, last_checked_at }`.
- `auto_approve`: `{ verdict, impact, risk, size, judged_at, summary }`.
- `pr.merged_at`: optional ISO timestamp when the PR was merged.
- `codex_session`: `{ id, last_prompt }`.
- `needs_user_input`: boolean flag (readers must validate this is a boolean and reject malformed values).
- `needs_user_input_payload`: optional JSON object used to carry handoff context (for example `{ "message": "...", "context": {...} }`).
- `stream_logs_stdout`: optional boolean snapshot of effective `run.log` stdout mirroring (`true`/`false`) for this run.
- `last_state`: cached derived state.
- `updated_at`: ISO timestamp.

`last_state` is derived from PR review status plus the single `needs_user_input` flag. CI/auto-approve data affects review-to-approved progression inside `WAITING_ON_REVIEW`.

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
- **State file (S)**: `run.json` is the persisted state file. `last_state` caches the derived state. The state file is the single source of truth.
- **Retry**: When the inner loop starts and finds an existing state file, it is resuming after a crash. Each state defines its own retry behavior for idempotent recovery.

#### States

| State | Description |
|-------|-------------|
| `RUNNING` | LLM executing the task with the current prompt context. |
| `NEEDS_INPUT` | LLM needs human input before continuing. |
| `WAITING_ON_REVIEW` | PR submitted. Polling for reviewer feedback. |
| `PR_APPROVED` | Manual review approval is present, or CI + auto-approve path is satisfied. Running merge and post-merge cleanup. |
| `DONE` | PR merged. Terminal state. |

#### State derivation
- `NEEDS_INPUT` if `needs_user_input == true`.
- `DONE` if a PR exists and `merged_at` is set (merged).
- `RUNNING` if no PR exists.
- `PR_APPROVED` if a PR exists and `review_status` is approved (manual approval path, unchanged).
- `PR_APPROVED` if a PR exists, `review_status` is not approved, `ci_status` is success, `auto_approve_enabled=true`, and `auto_approve.verdict=="APPROVE"` (additional auto-approve path).
- `WAITING_ON_REVIEW` if a PR exists and neither `PR_APPROVED` predicate above is met.

State derivation uses PR status plus the single `needs_user_input` flag; CI/auto-approve checks provide an additional path from `WAITING_ON_REVIEW` to `PR_APPROVED` when manual approval is not present.

Precedence rule: `NEEDS_INPUT` has priority over `DONE`; if `needs_user_input=true`, state is `NEEDS_INPUT` even when `pr.merged_at` is set.

#### Logic

**Initial entry (no state file):**
- LLM: start with prompt tagged `<state>RUNNING</state>`.
- Retry: resume the prior session ID and send the next state-tagged prompt through `codex exec resume <session_id>` when a prior session is recorded.

**Loop** (read `run.json`, derive state, dispatch):

- **If `NEEDS_INPUT`**: block for user handoff using `needs_user_input_payload`, then clear `needs_user_input` and `needs_user_input_payload`, persist, and resume LLM. Retry: still wait for input.
- **If PR submitted → `WAITING_ON_REVIEW`**: run review poll script. If changes requested AND `latest_review_submitted_at > review_addressed_at` (new review event), exec trigger:fix-pr and record `review_addressed_at`. Also, if status is open but a new feedback event is observed (`latest_review_submitted_at > review_addressed_at`) from the newest timestamp between `COMMENTED` PR review events and plain PR discussion comments, resume trigger:fix-pr and record `review_addressed_at` so the same feedback is not reprocessed.
  - Always poll CI and persist `pr.ci_status`.
  - If `review_status` is approved, derive `PR_APPROVED` via the manual path (unchanged).
  - If approval comes from an allowlisted plain PR comment, add a 👍 reaction to that comment in the polling path (idempotent best effort; failures are logged and do not block approval).
  - If `review_status` is not approved and `ci_status == success` and `auto_approve_enabled=true` and `run_record.auto_approve` is unset, run one direct auto-approve evaluation Codex turn (prompt instructs `$ag-judge`, judge book fixed to `references/jb.coding.md`) and persist `{ verdict, impact, risk, size, judged_at, summary }` on `RunRecord`.
  - In that additional path, `auto_approve.verdict == APPROVE` allows next-cycle transition to `PR_APPROVED`.
  - In that additional path, `auto_approve.verdict in {REJECT, ESCALATE}` remains blocked in `WAITING_ON_REVIEW` and does not re-run auto-approve.
- **If manual approval is present or the additional auto-approve path passes → `PR_APPROVED`**: run trigger:merge-pr. On success, derive DONE from `pr.merged_at`. Retry: re-run trigger (idempotent).
- **Bounded wait guardrail (review + approved states)**: `WAITING_ON_REVIEW` and `PR_APPROVED` use idle-poll escalation. If status polling fails repeatedly or the state does not progress for `max_idle_polls` consecutive polls (default `49`, about 45 minutes with the default backoff), force `NEEDS_INPUT` with a manual-guidance payload. Poll backoff grows from `initial_poll_seconds` (default `5s`) up to `max_poll_seconds` (default `60s`).

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

Inline additional path while in WAITING_ON_REVIEW when review is not already approved:
  - CI must be green (`pr.ci_status == success`)
  - if `auto_approve_enabled=true` and `auto_approve` is unset, run one direct `$ag-judge` evaluation turn
  - `auto_approve.verdict == APPROVE` allows transitioning to PR_APPROVED

From any non-DONE state:
  needs_user_input = true  ->  NEEDS_INPUT
```

Implementation note: `run_inner_loop` is a loop kernel that reads state, derives the
current state, dispatches to explicit per-state handlers, persists updates, and
schedules next poll timing. Handler boundaries in `loops/inner_loop.py` are:
`_handle_running_state`, `_handle_needs_input_state`,
`_handle_waiting_on_review_state`, and `_handle_pr_approved_state`.

### Retry behavior

| State | On crash / restart | Action |
|-------|--------------------|--------|
| `RUNNING` (no state file) | State file missing | Start fresh with prompt |
| `RUNNING` (session recorded) | Resume existing session | Resume session ID and send the next state-tagged prompt |
| `NEEDS_INPUT` | Still waiting | Re-enter wait |
| `WAITING_ON_REVIEW` | Polling interrupted | Continue polling PR status, including CI status updates and a single auto-approve evaluation per conversation when eligible |
| `PR_APPROVED` | Merge may be partial | Re-run trigger:merge-pr (idempotent); if merge remains stalled past idle threshold, escalate to `NEEDS_INPUT` |
| `DONE` | Terminal | Exit immediately |

### State handling

- `run.json` is authoritative for lifecycle state.
- Model output text is not authoritative for state transitions.
- For `NEEDS_INPUT`, the inner loop sets `needs_user_input=true`, persists `needs_user_input_payload`, and blocks on user handoff until a response is available.
- After handoff completes, inner loop clears `needs_user_input` and `needs_user_input_payload`, writes `run.json`, and resumes the state machine.

### Triggers

| Trigger | Invoked from | Description |
|---------|-------------|-------------|
| `trigger:fix-pr` | `WAITING_ON_REVIEW` | Resume Codex to address review feedback and update the PR. |
| `trigger:merge-pr` | `PR_APPROVED` | Merge the PR and run post-merge cleanup. Idempotent. |

Auto-approve evaluation is not a trigger. When eligible in `WAITING_ON_REVIEW`, the inner loop runs a dedicated Codex turn that directly instructs `$ag-judge`, then persists the verdict on `RunRecord.auto_approve`.

### Skill contract

The inner loop relies on a small explicit skill surface. These skills are part of the runtime contract, not optional guidance.

| Skill / contract | Where Loops invokes or enforces it | How it makes Loops work |
|---|---|---|
| `dev.do` | Base prompt for Codex turns (`RUNNING` and follow-up turns) | Keeps task execution in a single end-to-end implementation workflow (implement -> open/update PR -> continue until terminal state). |
| `a-review` | Base prompt contract (`RUNNING` only; exactly once per conversation) | Provides an in-turn quality gate before review polling; Loops requires posting the result to PR comments so reviewers and later turns share context. |
| `trigger:fix-pr` | `WAITING_ON_REVIEW` when new review/comment feedback is detected | Re-enters Codex to apply concrete reviewer feedback and updates `review_addressed_at` to prevent duplicate processing. |
| `$ag-judge` (direct, no trigger) | `WAITING_ON_REVIEW` when review is not approved, CI is green, and auto-approve is enabled | Produces one structured approval verdict (`APPROVE|REJECT|ESCALATE`) with impact/risk/size scores that Loops persists on `RunRecord.auto_approve` to decide whether `PR_APPROVED` is reachable without manual approval. |
| `trigger:merge-pr` | `PR_APPROVED` path and cleanup prompt | Performs merge/cleanup in an idempotent way; Loops keeps polling until `pr.merged_at` confirms transition to `DONE`. |
| `gen-notifier` (forbidden) | Base prompt hard guardrail | Prevents out-of-band desktop notification side effects from inside harness-managed runs; keeps Loops control flow deterministic and contained to run artifacts/logs. |

Practical invariant: if this skill contract changes, update `loops/inner_loop.py` prompt builders, `tests/test_inner_loop.py` prompt assertions, and related flow docs in `docs/flows/`.

### CLI callers

- `python -m loops` is the top-level wrapper CLI.
- `python -m loops init` initializes `.loops/` scaffolding (`jobs/`, logs, state, default config).
- `python -m loops doctor` upgrades config schema/default values in `config.json`.
- `python -m loops run` starts the outer loop runner.
- `python -m loops inner-loop` runs one inner-loop execution for a run directory.
- `python -m loops clean` deletes empty runs and archives completed runs.
- Direct module callers still work (`python -m loops.inner_loop`).

### Prompt catalog

The inner loop builds prompts in `loops/inner_loop.py` from a shared base template plus a state-specific suffix.

Base template (always present in Codex turns):

```text
Use dev.do to implement the task, open a PR, wait only for review from the a-review subagent, address feedback, and trigger:merge-pr when the state is exactly <state>PR_APPROVED</state>.
You are running inside the loops test harness. NEVER wait for human PR review/comments inside the agent; the harness monitors review activity and will re-invoke you when feedback arrives.
When you run a-review, always post its response to the PR comments. If there are no findings, explicitly post that no issues were found.
NEVER use the gen-notifier skill while running inside loops.
The current inner-loop state is passed via a trailing <state>...</state> tag; initial state is <state>RUNNING</state>.
If you need input from user, print what you need help with and end current conversation with <state>NEEDS_INPUT</>
When review is not already approved and CI is green, if auto-approve is enabled and no verdict exists yet, run $ag-judge once (judge book: references/jb.coding.md) and return one verdict: APPROVE, REJECT, or ESCALATE.
Do not merge until the state is exactly <state>PR_APPROVED</state>.
Task: [task_url]
```

Optional user-response block (added when a NEEDS_INPUT handoff response is available):

```text
User input:
[user_response]
```

Review prompts (used only when derived state is `WAITING_ON_REVIEW` and a new feedback event is detected):

1. Changes-requested review prompt:

```text
PR [pr_url] has changes requested. Address review feedback, update the PR, and summarize what changed.
<state>WAITING_ON_REVIEW</state>
```

2. Discussion-feedback prompt (plain PR comment or `COMMENTED` review):

```text
PR [pr_url] has new discussion comments. Review the feedback, address requested changes, update the PR, and summarize what changed. If there are no changes requested, summarize that and end the current turn.
<state>WAITING_ON_REVIEW</state>
```

3. Auto-approval evaluation prompt (inline within `WAITING_ON_REVIEW`):

```text
PR [pr_url] is not yet review-approved and has green CI. Run $ag-judge (judge book: references/jb.coding.md) against current diff, review threads, and CI evidence. Post the ag-judge verdict and impact/risk/size scores to the PR comments, then return one verdict plus impact/risk/size scores.
<state>WAITING_ON_REVIEW</state>
```

State-to-prompt mapping:

| Derived state | Prompt text added after base template | Final state tag |
|---|---|---|
| `RUNNING` | No additional suffix. | `<state>RUNNING</state>` |
| `WAITING_ON_REVIEW` (no new feedback event) | No Codex prompt is built; inner loop only polls PR state. | N/A |
| `WAITING_ON_REVIEW` (changes requested review) | `PR [pr_url] has changes requested. Address review feedback, update the PR, and summarize what changed.` | `<state>WAITING_ON_REVIEW</state>` |
| `WAITING_ON_REVIEW` (new discussion comments) | `PR [pr_url] has new discussion comments. Review the feedback, address requested changes, update the PR, and summarize what changed. If there are no changes requested, summarize that and end the current turn.` | `<state>WAITING_ON_REVIEW</state>` |
| `WAITING_ON_REVIEW` (auto-approve evaluation) | `PR [pr_url] is not yet review-approved and has green CI. Run $ag-judge (judge book: references/jb.coding.md) against current diff, review threads, and CI evidence. Post the ag-judge verdict and impact/risk/size scores to the PR comments, then return one verdict plus impact/risk/size scores.` | `<state>WAITING_ON_REVIEW</state>` |
| `PR_APPROVED` | `PR is approved. Run cleanup now and report completion.` | `<state>PR_APPROVED</state>` |
| `NEEDS_INPUT` | No Codex prompt is built while waiting. The handoff handler shows `needs_user_input_payload.message` to the user instead. | N/A |
| `DONE` | No prompt; loop exits. | N/A |

Prompt-related configuration and runtime inputs:

- `loops inner-loop --prompt-file PATH`: prepend file contents to every Codex prompt for the run.
- `inner_loop_runtime_config.json` (`env.LOOPS_PROMPT_FILE` / `env.CODEX_PROMPT_FILE`): run-scoped prompt-file source for outer-loop-launched runs when `--prompt-file` is unset.
- `LOOPS_PROMPT_FILE`: env fallback prompt file when `--prompt-file` is unset and runtime config does not provide a prompt path.
- `CODEX_PROMPT_FILE`: second env fallback when `LOOPS_PROMPT_FILE` is unset.
- `loop_config.auto_approve_enabled`: enables one-time auto-approve evaluation when review is not already approved.
- Auto-approve defaults are fixed in runtime design: require green CI and judge with `references/jb.coding.md`.
- `loop_config.handoff_handler` (`stdin_handler` or `gh_comment_handler`): changes where NEEDS_INPUT prompt messages are delivered.
- `task.url` in `run.json`: inserted into `Task: [task_url]`.
- Handoff response text: appended as `User input:` in the next Codex prompt.

## 7. PR review and merge gate handling

- When a PR is opened, the inner loop records it in `run.json`.
- The inner loop polls PR status and updates `pr.review_status`.
- When a review requests changes, the inner loop records `latest_review_submitted_at` (the review's `submittedAt` timestamp from GitHub) and invokes Codex to address the feedback. After Codex runs, `review_addressed_at` is set to `latest_review_submitted_at`. On subsequent polls, the loop only re-invokes Codex if `latest_review_submitted_at > review_addressed_at`, indicating a genuinely new review event. This prevents duplicate fix attempts when the reviewer has not yet re-reviewed.
- When status is still open (no formal review decision), the inner loop uses the newest timestamp between `COMMENTED` PR review and plain PR discussion comment events as its feedback signal. It uses the same `latest_review_submitted_at > review_addressed_at` guard to decide whether to resume Codex.
- Codex prompts must not ask the agent to wait for human review/comments inside a turn; the harness handles review polling/comment monitoring and re-invokes Codex when new feedback appears. The only in-turn waiting allowed is for the critical `a-review` subagent.
- Prompt contract requires posting `a-review` output to PR comments when `a-review` is run, including explicit no-findings comments.
- Prompt contract requires posting `ag-judge` verdict and impact/risk/size scores to PR comments during auto-approve evaluation turns.
- When approval is detected (GitHub review decision or an allowlisted approval signal newer than latest `CHANGES_REQUESTED` review), the loop derives `PR_APPROVED` via the existing manual path. If `task_provider_config.allowlist` is set, review polling first filters PR comments/reviews to allowlisted actors.
- If review is not already approved, CI is green, and `auto_approve_enabled=true` with no stored judgement on `RunRecord.auto_approve`, the loop runs one dedicated Codex evaluation turn that directly invokes `$ag-judge` (judge book fixed to `references/jb.coding.md`) and stores verdict/scores on `RunRecord`.
- When approval is detected from a plain PR comment signal, review polling adds a 👍 reaction on that approval comment (best effort, idempotent, and non-blocking).
- In that additional path, `auto_approve.verdict == APPROVE` allows transition to `PR_APPROVED`; `REJECT`/`ESCALATE` keep the run blocked in `WAITING_ON_REVIEW` with no auto re-run (single evaluation per conversation).

## 8. Error handling and recovery

- Non-fatal errors set `needs_user_input=true` and write the error message to `run.log`.
- Fatal errors still write to `run.json` and terminate the run.
- In `sync_mode=true`, if `Ctrl+C` interrupts an active inner-loop launch, the CLI prints a resume command (`loops inner-loop --run-dir <path>`) so the run can be resumed directly.
- On restart, the inner loop recomputes derived state from `run.json` and resumes accordingly.
- Repeated polling idleness in `WAITING_ON_REVIEW` or `PR_APPROVED` forces `NEEDS_INPUT` after the configured idle threshold.

## 9. Observability

### Logging
- Outer loop logs: `[LOOPS_ROOT]/oloops.log`.
- Outer loop per-task scheduling entries include the created inner-loop run directory path.
- Inner loop orchestration logs + Codex output mirror: `[INNER_LOOP_ROOT]/run.log`.
- Agent/Codex logs: `[INNER_LOOP_ROOT]/agent.log`.
- Log timestamps are local-time ISO-like strings without timezone suffix and fixed fractional precision.
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
