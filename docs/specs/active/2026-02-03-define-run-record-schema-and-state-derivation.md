# Execution Plan: Define run record schema and state derivation

**Date:** 2026-02-03
**Status:** Completed

---

## Goal

Implement the `run.json` schema and helpers to read/write it, plus a state-derivation function that computes `RUNNING | WAITING_ON_REVIEW | NEEDS_INPUT | DONE` from `pr.review_status` and `needs_user_input`, and caches `last_state` in `run.json`.

---

## Context

### Background
The inner loop persists run state under `.loops/` and needs a consistent schema plus helper utilities to read/write `run.json` and derive the cached `last_state` as defined in `DESIGN.md`.

### Current State
The repository only has `DESIGN.md` and `AGENTS.md`. No runtime code or tests exist yet.

### Constraints
- Python implementation with `pytest` tests.
- Follow the schema in `DESIGN.md`.
- Include required keys in `run.json`, even when optional fields are absent.

---

## Technical Approach

### Architecture/Design
Create a small `loops` Python package with dataclasses (or typed dicts) for `Task`, `RunPR`, `CodexSession`, and `RunRecord`. Provide helpers to derive run state, read `run.json`, and write `run.json` with cached `last_state` and updated timestamps.

### Technology Stack
- Python 3
- pytest

### Integration Points
- File system read/write for `run.json`.

### Design Patterns
- Dataclasses for structured data.
- Pure function for state derivation.

### Important Context
State derivation rules from `DESIGN.md`:
- `NEEDS_INPUT` if `needs_user_input` is true.
- `WAITING_ON_REVIEW` if PR exists and review status is not approved.
- `DONE` if PR exists, review status is approved, and `needs_user_input` is false.
- `RUNNING` otherwise.

---

## Steps

### Phase 1: Schema and Helpers
- [x] Create `loops` package structure.
- [x] Implement dataclasses and JSON serialization for run record.
- [x] Implement `derive_run_state` function.
- [x] Implement `read_run_record`/`write_run_record` helpers that ensure required keys and cache `last_state`.

### Phase 2: Tests
- [x] Add pytest coverage for the four required cases.
- [x] Verify `run.json` includes required keys and `last_state` is cached.

**Dependencies between phases:**
- Tests depend on helper implementations.

---

## Testing

- `python -m pytest`

---

## Dependencies

### External Services/APIs
- None

### Libraries/Packages
- pytest (for tests)

### Tools/Infrastructure
- None

### Access Required
- [ ] None

---

## Risks & Mitigations

| Risk | Impact | Probability | Mitigation Strategy |
|------|--------|-------------|---------------------|
| Ambiguity in optional fields | Low | Low | Include required keys with null values when absent |
| Missing test runtime dependency | Low | Medium | Note if pytest not installed in verification step |

---

## Questions

### Technical Decisions Needed
- [ ] None

### Clarifications Required
- [ ] None

### Research Tasks
- [ ] None

---

## Success Criteria

- [x] `run.json` schema matches `DESIGN.md` and includes required keys.
- [x] `derive_run_state` covers all required cases.
- [x] Tests cover the four specified scenarios.
- [x] Tests pass.

---

## Notes

- Default layout chosen: `loops/` package + `tests/`.
- Added CI workflow and Makefile during PR feedback fixes, plus stricter input validation.
