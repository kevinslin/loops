# Execution Plan: Manage inner loop state machine

**Date:** 2026-02-09
**Status:** In Progress

---

## Goal

Implement a durable inner-loop state machine that runs until `DONE`, keeps `run.json` (the state file **S**) authoritative under a single-writer model, supports model-initiated state transitions through a **signals skill**, and defines deterministic **retry** behavior for every state so the loop can recover from crashes.

---

## Context

### Background
The initial inner loop executed Codex once and only toggled `needs_user_input` on non-zero exits. The design required resumable control flow for review waiting, user handoff, cleanup, and crash recovery.

### Current State
- `loops/run_record.py` now supports validated `needs_user_input_payload`.
- `loops/state_signal.py` provides a validated signal queue entrypoint.
- `loops/inner_loop.py` now runs a multi-state orchestrator until derived `DONE`.
- Review-feedback Codex turns now include queued user handoff responses from `NEEDS_INPUT`.
- Integration tests validate lifecycle transitions, handoff, and resume behavior.

### Constraints
- `run.json` remains authoritative for state.
- State taxonomy follows `DESIGN.md`.
- `DONE` is derived from persisted PR merge state (`pr.merged_at`), not model output.
- Python-only implementation with no external dependencies added.

---

## Prerequisites

### 1. Signals skill
Allow the LLM to send signals to other programs. The model uses a skill (e.g. `$needs_input`) to enqueue structured intents into the run-local signal queue (`state_signals.jsonl`). The inner loop is the sole consumer of these signals and the sole writer of `run.json`.

### 2. State file (`run_state`)
**S** refers to the persisted run state in `run.json`. The `last_state` field caches the derived state. The state file is the single source of truth for the loop lifecycle; every state handler reads it at the top of the loop and writes it before yielding control.

### 3. Retry (crash recovery)
When the inner loop starts and encounters an existing state file, it is resuming after a crash or restart. Each state defines its own **retry** behavior — what the loop does when it wakes up and finds itself already in that state. This makes the loop crash-safe and idempotent.

---

## State Machine

### States

| State | Description |
|-------|-------------|
| `START` | LLM has been launched with the initial prompt. Session is being established. |
| `NEEDS_INPUT` | LLM needs input from a human before it can continue. |
| `WAITING_ON_REVIEW` | A PR has been submitted. Polling for reviewer feedback. |
| `PR_APPROVED` | PR has been approved. Running merge and post-merge cleanup. |
| `DONE` | PR merged. Terminal state. |

### Logic

#### Initial entry: no state file

When no state file exists, this is a fresh start.

- **LLM**: Start with prompt. Send signal to set **S:START**, payload: `{ sessionID }`.
- **Retry** (crash during start): Resume session ID with prompt `"continue"`.

#### Loop

The inner loop reads `run.json`, derives state, and dispatches to the matching handler. Each iteration:

##### If `NEEDS_INPUT`
- Send signal **S:NEEDS_INPUT**, payload: `{ questions }`.
- Block until user provides a response.
- On response: clear `needs_user_input`, persist answer, resume LLM with user input.
- **Retry** (crash while waiting): Still wait for input. The state file already records `NEEDS_INPUT`, so the loop re-enters the same handler.

##### If PR submitted → `WAITING_ON_REVIEW`
- Set **S:WAITING_ON_REVIEW**.
- Run poll script to check PR review status (fetches `latestReviews` from GitHub to get `latest_review_submitted_at`).
- If reviewer requested changes AND `latest_review_submitted_at > review_addressed_at` (new review event): exec **trigger:fix-pr** (resume Codex to address feedback), then record `review_addressed_at = latest_review_submitted_at`.
- If reviewer requested changes but `latest_review_submitted_at <= review_addressed_at`: skip re-invocation, continue polling with backoff (already addressed this review round).
- If reviewer approved: transition to `PR_APPROVED`.
- **Retry** (crash while polling): Continue polling. State file already says `WAITING_ON_REVIEW`.

##### If PR approved → `PR_APPROVED`
- Set **S:PR_APPROVED**.
- Run **trigger:merge-pr** (merge the PR and run post-merge cleanup).
- On success: derive `DONE` from `pr.merged_at`.
- **Retry** (crash during cleanup): Continue. Re-run merge trigger (idempotent).

### State transition diagram

```text
                         (no state file)
                               |
                               v
                        +--------------+
                        |    START     |
                        +--------------+
                               |
                               | LLM sends signal S:START
                               | payload: { sessionID }
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
          |         |     +------------------+   (back to RUNNING
          v         |     |   PR_APPROVED    |    or WAITING_ON_REVIEW)
  trigger:fix-pr    |     +------------------+
  (back to          |           |
   WAITING_ON_REVIEW)     |           | trigger:merge-pr
                    |           | pr.merged_at set
                    |           v
                    |     +-------------+
                    +---->|    DONE     |
                          +-------------+

From any non-DONE state:
  needs_user_input = true  ->  NEEDS_INPUT
```

### Retry behavior summary

| State | On crash / restart | Action |
|-------|--------------------|--------|
| `START` (no state file) | State file missing | Start fresh with prompt |
| `START` (state file exists, session recorded) | Resume existing session | Resume session ID with prompt `"continue"` |
| `NEEDS_INPUT` | Still waiting for input | Re-enter wait; do not re-send signal |
| `WAITING_ON_REVIEW` | Polling was interrupted | Continue polling PR status |
| `PR_APPROVED` | Merge may be partial | Re-run trigger:merge-pr (idempotent) |
| `DONE` | Terminal | Exit immediately |

---

## Technical Approach

### Architecture/Design
- Implemented orchestrator loop in `run_inner_loop` that repeatedly reads persisted state and executes a state handler until `DONE`.
- Enforced single-writer model for `run.json`: only inner loop writes run state.
- Added signal queue channel (`state_signals.jsonl`) for model-triggered `NEEDS_INPUT` intents.
- Added signal offset tracking (`state_signals.offset`) to consume append-only signals safely.
- Added blocking `NEEDS_INPUT` handler that collects user response, persists, and feeds response into next Codex prompt.
- Triggers (`trigger:fix-pr`, `trigger:merge-pr`) encapsulate discrete actions the loop can invoke in response to state transitions.

### Technology Stack
- Python 3
- pytest

### Integration Points
- `loops/run_record.py`: payload schema + validation + persistence.
- `loops/state_signal.py`: enqueue validated signals.
- `loops/inner_loop.py`: orchestrator, signal consumption, PR polling, cleanup, handoff.
- `tests/`: integration and validation coverage.

### Design Patterns
- Deterministic FSM with derived state (`derive_run_state`).
- Command/query separation: signal script writes intent; inner loop applies intent to state.
- Crash-safe behavior from persisted run state and resumable control loop.
- Each state defines its own retry semantics for idempotent recovery.

### Important Context
- `_handle_state` behavior is encoded in explicit state branches inside `run_inner_loop`.
- `NEEDS_INPUT` blocks until handoff returns a non-empty response.
- `PR_APPROVED` runs trigger:merge-pr once per PR URL, then continues polling for merge.
- Default inner-loop prompt includes: `If needing input from user, use "$needs_input" skill to request user input.`

---

## Steps

### Phase 1: Extend run record for state-machine payloads
- [x] Add optional `needs_user_input_payload` (JSON object) to `RunRecord`.
- [x] Add validation rules for payload schema/size and malformed values.
- [x] Keep `derive_run_state` unchanged (still based on `pr` + `needs_user_input`).
- [x] Add tests for round-trip persistence and validation errors.

### Phase 2: Add model-accessible signal channel (no direct state writes)
- [x] Implement script entrypoint to enqueue `NEEDS_INPUT` payloads to run-local signal queue.
- [x] Reject unsupported states and malformed payloads.
- [x] Add audit log entries for accepted/applied signals.
- [x] Add tests for signal channel validation behavior.

### Phase 3: Implement inner-loop orchestrator + handlers
- [x] Refactor `run_inner_loop` into a persistent state loop.
- [x] Ensure `PROMPT_TEMPLATE` includes explicit `$needs_input` instruction.
- [x] `RUNNING` / `START`: invoke Codex, persist session/output-derived PR metadata.
- [x] `WAITING_ON_REVIEW`: poll PR status with backoff and persist changes.
- [x] `NEEDS_INPUT`: block for user handoff, persist response, clear input flag.
- [x] `PR_APPROVED`: run trigger:merge-pr and continue polling for merged state.
- [x] Add guardrails (max iterations, idle poll escalation to `NEEDS_INPUT`, structured logs).

### Phase 4: Add explicit retry/crash recovery
- [ ] Ensure START state detects existing session and resumes with `"continue"` prompt.
- [ ] Ensure NEEDS_INPUT retry re-enters wait without re-sending signal.
- [ ] Ensure WAITING_ON_REVIEW retry continues polling without side effects.
- [ ] Ensure PR_APPROVED retry re-runs trigger:merge-pr idempotently.
- [ ] Add integration tests for crash-resume scenarios per state.

### Phase 5: Verification and docs
- [x] Add integration tests for lifecycle transitions to `DONE`.
- [x] Add resume test starting from `WAITING_ON_REVIEW`.
- [ ] Update DESIGN.md with retry semantics and trigger descriptions.
- [ ] Run full test suite and capture results.

**Dependencies between phases:**
- Phase 2 depends on Phase 1 schema changes.
- Phase 3 depends on Phases 1-2 primitives.
- Phase 4 depends on Phase 3 orchestrator.
- Phase 5 depends on Phases 1-4 completion.

---

## Testing

- `python -m pytest tests/test_run_record.py tests/test_state_signal.py tests/test_inner_loop.py`
- `python -m pytest`

Result:
- 42 passed (full suite), 0 failed.

---

## Dependencies

### External Services/APIs
- Optional GitHub CLI (`gh`) for runtime PR polling in non-test environments.

### Libraries/Packages
- None (stdlib + existing pytest stack).

### Tools/Infrastructure
- Codex CLI via `CODEX_CMD`.

### Access Required
- [x] No new credentials required for test execution.

---

## Risks & Mitigations

| Risk | Impact | Probability | Mitigation Strategy |
|------|--------|-------------|---------------------|
| Handoff can block indefinitely waiting for user response | High | Med | Escalation path via persisted `NEEDS_INPUT` + heartbeat logging |
| Invalid model payloads | Med | Med | Strict payload validation in run record and signal script |
| Busy-looping in review wait state | Med | Med | Backoff + max idle polls + escalation to `NEEDS_INPUT` |
| Premature terminal state from model text | High | Low | `DONE` derived only from persisted merged PR metadata |
| Crash during cleanup leaves partial merge | Med | Low | trigger:merge-pr is idempotent; retry re-runs safely |

---

## Questions

### Technical Decisions Needed
- [x] `NEEDS_INPUT` behavior: block until user response.
- [x] `run.json` ownership model: single writer (inner loop only).
- [x] Signal scope: `NEEDS_INPUT` only for MVP.
- [x] Payload field name: `needs_user_input_payload`.
- [ ] START state: explicit state vs. derived from absence of session. Current impl derives RUNNING; consider adding explicit START.

### Clarifications Required
- [x] Payload schema: `{ "message": string, "context"?: object }`.

### Research Tasks
- [x] Reused existing runtime surface; no additional provider utility required in this phase.

---

## Success Criteria

- [x] Inner loop runs state machine until derived `DONE`.
- [x] `run.json` remains authoritative and resumable.
- [x] Model requests input through validated signal channel.
- [x] Inner loop is sole writer of `run.json`.
- [x] `NEEDS_INPUT` blocks until user response.
- [x] Review waiting uses polling/backoff (no tight Codex loop).
- [x] No direct model-declared `DONE` path exists.
- [x] Integration tests cover lifecycle, handoff, and restart paths.
- [ ] Each state has defined, tested retry/crash recovery behavior.
- [ ] Triggers (fix-pr, merge-pr) are encapsulated and idempotent.

---

## Notes

- Simplification kept: model output is advisory; persisted state drives lifecycle.
- State names aligned with current implementation: `WAITING_ON_REVIEW` and `PR_APPROVED`.
- Triggers (`trigger:fix-pr`, `trigger:merge-pr`) are named actions invoked by the loop, not model-initiated.
- Implemented files:
  - `loops/run_record.py`
  - `loops/state_signal.py`
  - `loops/inner_loop.py`
  - `tests/test_run_record.py`
  - `tests/test_state_signal.py`
  - `tests/test_inner_loop.py`
