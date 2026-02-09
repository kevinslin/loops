# Execution Plan: Manage inner loop state machine

**Date:** 2026-02-09
**Status:** Completed

---

## Goal

Implement a durable inner-loop state machine that runs until derived `DONE`, keeps `run.json` authoritative under a single-writer model, and supports model-initiated `NEEDS_INPUT` requests through a validated signal script consumed by the inner loop.

---

## Context

### Background
The initial inner loop executed Codex once and only toggled `needs_user_input` on non-zero exits. The design required resumable control flow for review waiting, user handoff, cleanup, and crash recovery.

### Current State
- `loops/run_record.py` now supports validated `needs_user_input_payload`.
- `loops/state_signal.py` provides a validated signal queue entrypoint.
- `loops/inner_loop.py` now runs a multi-state orchestrator until derived `DONE`.
- Integration tests validate lifecycle transitions, handoff, and resume behavior.

### Constraints
- `run.json` remains authoritative for state.
- State taxonomy follows `DESIGN.md`: `RUNNING | WAITING_ON_REVIEW | NEEDS_INPUT | PR_APPROVED | DONE`.
- `DONE` is derived from persisted PR merge state (`pr.merged_at`), not model output.
- Python-only implementation with no external dependencies added.

---

## Technical Approach

### Architecture/Design
- Implemented orchestrator loop in `run_inner_loop` that repeatedly reads persisted state and executes a state handler until `DONE`.
- Enforced single-writer model for `run.json`: only inner loop writes run state.
- Added signal queue channel (`state_signals.jsonl`) for model-triggered `NEEDS_INPUT` intents.
- Added signal offset tracking (`state_signals.offset`) to consume append-only signals safely.
- Added blocking `NEEDS_INPUT` handler that collects user response, persists, and feeds response into next Codex prompt.

### Technology Stack
- Python 3
- pytest

### Integration Points
- `loops/run_record.py`: payload schema + validation + persistence.
- `loops/state_signal.py`: enqueue validated `NEEDS_INPUT` signals.
- `loops/inner_loop.py`: orchestrator, signal consumption, PR polling, cleanup, handoff.
- `tests/`: integration and validation coverage.

### Design Patterns
- Deterministic FSM with derived state (`derive_run_state`).
- Command/query separation: signal script writes intent; inner loop applies intent to state.
- Crash-safe behavior from persisted run state and resumable control loop.

### Important Context
- `_handle_state` behavior is encoded in explicit state branches inside `run_inner_loop`.
- `NEEDS_INPUT` blocks until handoff returns a non-empty response.
- `PR_APPROVED` runs cleanup once per PR URL, then continues polling for merge.
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
- [x] `RUNNING`: invoke Codex, persist session/output-derived PR metadata.
- [x] `WAITING_ON_REVIEW`: poll PR status with backoff and persist changes.
- [x] `NEEDS_INPUT`: block for user handoff, persist response, clear input flag.
- [x] `PR_APPROVED`: run cleanup and continue polling for merged state.
- [x] Add guardrails (max iterations, idle poll escalation to `NEEDS_INPUT`, structured logs).

### Phase 4: Verification and docs
- [x] Add integration tests for lifecycle transitions to `DONE`.
- [x] Add resume test starting from `WAITING_ON_REVIEW`.
- [x] Update active spec with final decisions and completion status.
- [x] Run full test suite and capture results.

**Dependencies between phases:**
- Phase 2 depends on Phase 1 schema changes.
- Phase 3 depends on Phases 1-2 primitives.
- Phase 4 depends on Phases 1-3 completion.

---

## Testing

- `python -m pytest tests/test_run_record.py tests/test_state_signal.py tests/test_inner_loop.py`
- `python -m pytest`

Result:
- 33 passed (full suite), 0 failed.

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

---

## Questions

### Technical Decisions Needed
- [x] `NEEDS_INPUT` behavior: block until user response.
- [x] `run.json` ownership model: single writer (inner loop only).
- [x] Signal scope: `NEEDS_INPUT` only for MVP.
- [x] Payload field name: `needs_user_input_payload`.

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

---

## Notes

- Simplification kept: model output is advisory; persisted state drives lifecycle.
- Implemented files:
  - `loops/run_record.py`
  - `loops/state_signal.py`
  - `loops/inner_loop.py`
  - `tests/test_run_record.py`
  - `tests/test_state_signal.py`
  - `tests/test_inner_loop.py`
