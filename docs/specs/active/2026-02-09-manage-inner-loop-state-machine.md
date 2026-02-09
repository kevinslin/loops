# Execution Plan: Manage inner loop state machine

**Date:** 2026-02-09
**Status:** Planning

---

## Goal

Implement a durable inner-loop state machine that runs until derived `DONE`, keeps `run.json` authoritative under a single-writer model, and supports model-initiated `NEEDS_INPUT` requests through a validated signal script consumed by the inner loop.

---

## Context

### Background
The current inner loop executes Codex once per invocation and only toggles `needs_user_input` on command failure. The design requires a resumable lifecycle that handles PR review waiting, user handoff, and cleanup completion.

### Current State
- `loops/run_record.py` derives state from `pr` + `needs_user_input` and caches `last_state`.
- `loops/inner_loop.py` executes a single Codex run and updates `codex_session`.
- No orchestrated state loop exists yet (`RUNNING -> WAITING_ON_REVIEW -> PR_APPROVED -> DONE` etc.).
- No script exists for model-triggered state updates with payload.

### Constraints
- `run.json` is the single source of truth for runtime state.
- Use state taxonomy from `DESIGN.md`: `RUNNING | WAITING_ON_REVIEW | NEEDS_INPUT | PR_APPROVED | DONE`.
- `DONE` must be derived from persisted PR facts (merged), not model-declared output.
- Keep implementation in Python; avoid new external dependencies.

---

## Technical Approach

### Architecture/Design
- Add an inner-loop orchestrator that repeats until `derive_run_state(record.pr, record.needs_user_input) == "DONE"`.
- Introduce a state-handling dispatcher: `_handle_state(state, state_args, record)` returns the next prompt or a wait action.
- Persist state transitions by writing `run.json` on every loop iteration and after every side effect (Codex call, PR poll result, user handoff result).
- Enforce a single-writer contract: only the inner-loop orchestrator writes `run.json`.
- Add a small CLI signal script that the model can call to request `NEEDS_INPUT` with payload; script validates input and appends to a run-local signal queue.
- Inner loop consumes queued signals and applies them to `run.json` via `run_record` helpers.
- Treat model output as untrusted text; the authoritative transition signal is only persisted `run.json` data.

### Technology Stack
- Python 3
- pytest

### Integration Points
- `loops/inner_loop.py`: convert from single-run executor to orchestrated state machine runner.
- `loops/run_record.py`: add optional persisted payload for handoff context and strict validation.
- New script module (e.g., `loops/state_signal.py`) for model-initiated signal enqueueing.
- Existing PR metadata in `run.json.pr` for transition derivation.

### Design Patterns
- Deterministic FSM with pure state derivation and side-effect adapters.
- Command/query separation: model writes intent via signal script; loop consumes signals and updates persisted state.
- Crash-safe persistence by frequent, explicit `run.json` writes.

### Important Context
- `_handle_state` owns waiting logic. In `WAITING_ON_REVIEW`, it polls PR status and sleeps/backoffs instead of repeatedly invoking Codex.
- `NEEDS_INPUT` is set by the inner loop after processing a signal (or runtime failure); when in `NEEDS_INPUT`, the loop blocks until user handoff returns a response, then clears the flag and continues.
- `PR_APPROVED` triggers cleanup flow; `DONE` is reached only after merged PR state is observed in `run.json`.
- Default inner-loop prompt template must include: `If needing input from user, use "$needs_input" skill to request user input.`

---

## Steps

### Phase 1: Extend run record for state-machine payloads
- [ ] Add optional `needs_user_input_payload` (JSON object) to `RunRecord`.
- [ ] Add validation rules for payload shape/size and rejection of malformed values.
- [ ] Keep `derive_run_state` unchanged (still based on `pr` + `needs_user_input`).
- [ ] Add tests for round-trip persistence and validation errors.

### Phase 2: Add model-accessible signal channel (no direct state writes)
- [ ] Implement script entrypoint to enqueue `NEEDS_INPUT` with payload to a run-local signal queue.
- [ ] Reject unsupported signal types and malformed payloads.
- [ ] Add audit log entry in `run.log` when signals are accepted and when applied.
- [ ] Add tests for accepted/rejected signal commands and queue parsing.

### Phase 3: Implement inner-loop orchestrator + handlers
- [ ] Refactor `run_inner_loop` into a loop: read state -> `_handle_state` -> optional Codex invocation -> persist -> repeat.
- [ ] Ensure `PROMPT_TEMPLATE` includes explicit instruction to use `$needs_input` when user input is required.
- [ ] Implement handler behavior per state:
- [ ] `RUNNING`: invoke/resume Codex and persist outputs/session.
- [ ] `WAITING_ON_REVIEW`: poll PR status with wait/backoff and update `pr.review_status`.
- [ ] `NEEDS_INPUT`: hand off to user with payload context; block until response, persist response, and clear input flag.
- [ ] `PR_APPROVED`: run cleanup path, then continue polling for merge to reach `DONE`.
- [ ] Add guardrails: max idle polls, structured logging, and safe retries on transient errors.

### Phase 4: Verification and docs
- [ ] Add integration tests for full lifecycle transitions to `DONE`.
- [ ] Add restart/resume tests proving crash recovery from persisted `run.json`.
- [ ] Update `DESIGN.md` and/or active spec notes if field names or transition details change.
- [ ] Run full test suite and record results.

**Dependencies between phases:**
- Phase 2 depends on Phase 1 schema changes.
- Phase 3 depends on Phases 1-2 primitives.
- Phase 4 depends on Phases 1-3 completion.

---

## Testing

- Integration test: `RUNNING -> WAITING_ON_REVIEW -> PR_APPROVED -> DONE` using stubs for Codex + PR polling.
- Integration test: `RUNNING -> NEEDS_INPUT -> RUNNING` with persisted payload and user handoff response.
- Recovery test: restart mid-`WAITING_ON_REVIEW` and verify loop resumes from `run.json`.
- Validation test: signal script rejects malformed payload and unsupported signal types.
- Regression test: existing `derive_run_state` behavior remains unchanged.
- Full suite: `python -m pytest`.

---

## Dependencies

### External Services/APIs
- GitHub PR metadata source (existing project provider/client) for review/merge polling.

### Libraries/Packages
- None (stdlib + existing test stack).

### Tools/Infrastructure
- Codex CLI via `CODEX_CMD`.

### Access Required
- [ ] No new credentials beyond existing GitHub/Codex setup.

---

## Risks & Mitigations

| Risk | Impact | Probability | Mitigation Strategy |
|------|--------|-------------|---------------------|
| Handoff can block indefinitely waiting for user response | High | Med | Add heartbeat logging, timeout alerts, and explicit operator resume procedure |
| Model writes invalid or oversized payloads | Med | Med | Strict schema validation, size limits, and clear error return from script |
| Busy-looping while waiting for review | Med | Med | Backoff + sleep in `WAITING_ON_REVIEW`, with periodic heartbeat logs |
| Premature terminal state due to model instruction | High | Low | Disallow direct `DONE` writes; only derived `DONE` from `pr.merged_at` |

---

## Questions

### Technical Decisions Needed
- [x] `NEEDS_INPUT` behavior: block until user response is received.
- [x] `run.json` ownership model: single writer (inner loop process only).
- [x] Signal scope for MVP: support `NEEDS_INPUT` only.
- [x] Canonical payload field name: `needs_user_input_payload`.

### Clarifications Required
- [x] User-handoff payload schema (MVP): `{ "message": string, "context"?: object }`.

### Research Tasks
- [ ] Confirm best existing PR polling utility in repo to reuse (avoid duplicate API logic).

---

## Success Criteria

- [ ] Inner loop process continues until derived `DONE`.
- [ ] `run.json` remains authoritative for all transitions and restart recovery.
- [ ] Model can request `NEEDS_INPUT` + payload only through validated signal script.
- [ ] Inner loop is the only process that writes `run.json`.
- [ ] `NEEDS_INPUT` path blocks until user response is provided.
- [ ] `WAITING_ON_REVIEW` uses polling/backoff and does not repeatedly invoke Codex.
- [ ] No path allows direct model-declared `DONE`.
- [ ] Integration tests cover lifecycle, handoff, and restart scenarios.

---

## Notes

- Simplification applied: removed XML `<exit>...</exit>` as the lifecycle authority. Control plane is persisted `run.json`; model output is only advisory.
- Simplification applied: scope model signals to `NEEDS_INPUT` first, avoiding a generic model-driven transition API.
