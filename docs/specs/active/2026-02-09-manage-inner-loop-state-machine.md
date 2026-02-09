# Execution Plan: Manage inner loop state machine

**Date:** 2026-02-09
**Status:** Planning

---

## Goal

Implement a durable inner-loop state machine that runs until derived `DONE`, keeps `run.json` authoritative, and supports model-initiated `NEEDS_INPUT` requests through a validated state-writer script.

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
- Add a small CLI script that the model can call to request `NEEDS_INPUT` with payload; script validates inputs and writes through `run_record` helpers.
- Treat model output as untrusted text; the authoritative transition signal is only persisted `run.json` data.

### Technology Stack
- Python 3
- pytest

### Integration Points
- `loops/inner_loop.py`: convert from single-run executor to orchestrated state machine runner.
- `loops/run_record.py`: add optional persisted payload for handoff context and strict validation.
- New script module (e.g., `loops/state_signal.py`) for model-initiated state writes.
- Existing PR metadata in `run.json.pr` for transition derivation.

### Design Patterns
- Deterministic FSM with pure state derivation and side-effect adapters.
- Command/query separation: model writes intent via script; loop consumes persisted state.
- Crash-safe persistence by frequent, explicit `run.json` writes.

### Important Context
- `_handle_state` owns waiting logic. In `WAITING_ON_REVIEW`, it polls PR status and sleeps/backoffs instead of repeatedly invoking Codex.
- `NEEDS_INPUT` is set via state-writer script or runtime failures; user response clears the flag and is fed back into the next Codex prompt.
- `PR_APPROVED` triggers cleanup flow; `DONE` is reached only after merged PR state is observed in `run.json`.

---

## Steps

### Phase 1: Extend run record for state-machine payloads
- [ ] Add optional `needs_user_input_payload` (JSON object) to `RunRecord`.
- [ ] Add validation rules for payload shape/size and rejection of malformed values.
- [ ] Keep `derive_run_state` unchanged (still based on `pr` + `needs_user_input`).
- [ ] Add tests for round-trip persistence and validation errors.

### Phase 2: Add model-accessible state writer
- [ ] Implement script entrypoint to write `NEEDS_INPUT` with payload to `run.json`.
- [ ] Reject unsupported direct state writes (especially direct `DONE` writes).
- [ ] Add audit log entry in `run.log` when state writer updates record.
- [ ] Add tests for accepted/rejected state-writer commands.

### Phase 3: Implement inner-loop orchestrator + handlers
- [ ] Refactor `run_inner_loop` into a loop: read state -> `_handle_state` -> optional Codex invocation -> persist -> repeat.
- [ ] Implement handler behavior per state:
- [ ] `RUNNING`: invoke/resume Codex and persist outputs/session.
- [ ] `WAITING_ON_REVIEW`: poll PR status with wait/backoff and update `pr.review_status`.
- [ ] `NEEDS_INPUT`: hand off to user with payload context; persist response and clear input flag.
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
- Validation test: state-writer rejects direct `DONE` or malformed payload.
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
| Concurrent writes to `run.json` from loop + state-writer script | High | Med | Centralize writes via helpers and use atomic file replacement semantics |
| Model writes invalid or oversized payloads | Med | Med | Strict schema validation, size limits, and clear error return from script |
| Busy-looping while waiting for review | Med | Med | Backoff + sleep in `WAITING_ON_REVIEW`, with periodic heartbeat logs |
| Premature terminal state due to model instruction | High | Low | Disallow direct `DONE` writes; only derived `DONE` from `pr.merged_at` |

---

## Questions

### Technical Decisions Needed
- [ ] Choose canonical payload field name (`needs_user_input_payload` vs `state_args`).
- [ ] Decide whether state-writer script should support only `NEEDS_INPUT` (recommended) or additional non-terminal states.

### Clarifications Required
- [ ] Define exact user-handoff payload schema (required keys + optional metadata).

### Research Tasks
- [ ] Confirm best existing PR polling utility in repo to reuse (avoid duplicate API logic).

---

## Success Criteria

- [ ] Inner loop process continues until derived `DONE`.
- [ ] `run.json` remains authoritative for all transitions and restart recovery.
- [ ] Model can request `NEEDS_INPUT` + payload only through validated script.
- [ ] `WAITING_ON_REVIEW` uses polling/backoff and does not repeatedly invoke Codex.
- [ ] No path allows direct model-declared `DONE`.
- [ ] Integration tests cover lifecycle, handoff, and restart scenarios.

---

## Notes

- Simplification applied: removed XML `<exit>...</exit>` as the lifecycle authority. Control plane is persisted `run.json`; model output is only advisory.
- Simplification applied: scope model-writable state to `NEEDS_INPUT` first, avoiding a generic model-driven transition API.
