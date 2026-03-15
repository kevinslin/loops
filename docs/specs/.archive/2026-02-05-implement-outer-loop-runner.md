# Execution Plan: Implement outer loop runner

**Date:** 2026-02-05
**Status:** Completed

---

## Goal

Implement the outer loop runner that loads config, polls the task provider, filters by ready status, de-dupes via `outer_state.json`, creates per-task run directories and `run.json`, and starts the inner loop. Honor `poll_interval_seconds` and `force` behavior.

---

## Context

### Background
The repo has the run record helpers and the GitHub Projects V2 provider implemented, but there is no outer loop runner yet. We need to connect these pieces to drive tasks end-to-end.

### Current State
- `loops/run_record.py` defines the run record schema and helpers.
- `loops/providers/github_projects_v2.py` implements the TaskProvider.
- No outer loop runner, config loader, or outer loop tests exist.

### Constraints
- Python implementation.
- Follow `DESIGN.md` for storage layout and state transitions.
- Keep outer loop state under `.loops/` and do not reprocess tasks unless `force=true`.

---

## Technical Approach

### Architecture/Design
- Add a new `loops/outer_loop.py` module with:
  - `OuterLoopConfig` dataclass (defaults per `DESIGN.md`).
  - `LoopsConfig` dataclass for config file parsing (`provider_id`, `loop_config`, `provider_config`, optional `inner_loop_config`).
  - `OuterLoopState` read/write helpers for `.loops/outer_state.json` (store processed task keys + metadata).
  - `OuterLoopRunner` that supports `run_once()` and `run_forever()`.
- Provide a minimal inner loop launcher hook (callable) so tests can stub it; default behavior can write a placeholder log or raise if missing.
- Provide a config loader for JSON files and a simple CLI entrypoint (optional) for running the outer loop from a config path.

### Technology Stack
- Python 3
- pytest

### Integration Points
- `loops.run_record.write_run_record` for `run.json` creation.
- `loops.providers.github_projects_v2.GithubProjectsV2TaskProvider` for polling.
- File system for `.loops/outer_state.json` and `.loops/oloops.log`.

### Design Patterns
- Dataclasses for configs/state.
- Dependency injection for provider + inner loop handler for testability.

### Important Context
- `INNER_LOOP_ROOT` naming: `{yyyy-mm-dd}-{task_title_kebab_case}-{task_id}` under `.loops/`.
- `outer_state.json` is the dedupe ledger; skip tasks already seen unless `force=true`.
- `emit_on_first_run` should skip launching tasks on first run when false (baseline only).

---

## Steps

### Phase 1: Outer loop core
- [x] Implement config dataclasses and JSON loader.
- [x] Implement outer state read/write helpers.
- [x] Implement task filtering + run directory creation + `run.json` write.
- [x] Add inner loop launcher hook and logging.

### Phase 2: Tests
- [x] Add tests for run directory creation and run.json contents with a stub provider.
- [x] Add tests for dedupe behavior and `force=true`.
- [x] Add test for `emit_on_first_run=false` baseline behavior.

**Dependencies between phases:**
- Tests depend on outer loop implementation.

---

## Testing

- `python -m pytest`

---

## Dependencies

### External Services/APIs
- None (tests use stubs).

### Libraries/Packages
- pytest

### Tools/Infrastructure
- None

### Access Required
- [ ] None

---

## Risks & Mitigations

| Risk | Impact | Probability | Mitigation Strategy |
|------|--------|-------------|---------------------|
| Ambiguity in config format | Med | Med | Keep JSON schema simple + document in code comments/tests |
| Task id contains unsafe filename characters | Low | Med | Sanitize slug components for run directory names |
| Missing inner loop implementation | Low | High | Provide injectable launcher; default placeholder |

---

## Questions

### Technical Decisions Needed
- [x] Should we provide a CLI entrypoint now, or keep the runner import-only?
  - Answer: Provide a Click-based CLI entrypoint.

### Clarifications Required
- [x] Is JSON the preferred config format and location (default `.loops/config.json`)?
  - Answer: Yes, default to `.loops/config.json`.

### Research Tasks
- [ ] None

---

## Success Criteria

- [x] Outer loop creates run directories + `run.json` for ready tasks.
- [x] `outer_state.json` is updated and dedupe works across polls unless `force=true`.
- [x] `emit_on_first_run=false` skips launching tasks on first run but records state.
- [x] Tests cover the validation scenario described in the issue.

---

## Notes

- Simplified scope to a single provider + JSON config loader (no YAML) to keep MVP focused and aligned with recent repo changes.
- Confirmed CLI entrypoint with Click and default config path `.loops/config.json`.
