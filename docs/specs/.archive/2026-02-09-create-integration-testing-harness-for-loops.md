# Execution Plan: Create Integration Testing Harness for Loops

**Date:** 2026-02-09
**Status:** Completed

---

## Goal

Create a dedicated live integration harness that uses real GitHub API calls and real Codex execution to validate Loops can pick up ready tasks from the specified GitHub Projects v2 board. The integration tests live in the Loops repo; `kevinslin/loops-integ` is the target repo for test issues.

---

## Context

### Background
We need an end-to-end integration test that validates the GitHub Projects v2 provider and outer-loop behavior using real API calls and real Codex runs. The test must initialize two dummy tasks and confirm the outer loop picks up task1 only.

### Current State
- Loops already has a GitHub Projects v2 provider and an outer loop runner.
- Provider polling supports deterministic server-side filters (`repository=...`, `tag=...`).
- The CLI command is `loops run --run-once` (legacy `python -m loops --run-once` still works).
- Run directories are materialized under `.loops/jobs/`.
- Existing tests are unit-focused; no live integration harness exists.

### Constraints
- Use the board at `https://github.com/users/kevinslin/projects/6/views/1`.
- Use real API calls.
- Keep target repo hardcoded as `kevinslin/loops-integ` (for now).
- Add integration tests under `tests/integ/` in the Loops repo.
- Integration test must:
  - Create two dummy tasks:
    - task1: add a test file "hello"
    - task2: add a test file "bye"
  - Set task1 to `Todo` and task2 to non-ready.
  - Configure provider filters so polling is deterministic for only test-created tasks.
  - Verify the outer loop picks up task1.
  - Verify Codex is actually invoked (no stub inner loop).
- Integration test should be runnable both manually and in CI (with explicit opt-in).

---

## Technical Approach

### Architecture/Design
- Add a new live integration harness under `tests/integ/` in the Loops repo.
- Use GitHub's API (via `gh api graphql`) to create issues in `kevinslin/loops-integ`, add them to the target project, and set status values.
- Tag both issues with a unique per-test-run label and configure `provider_config.filters`:
  - `repository=kevinslin/loops-integ`
  - `tag=<unique-integ-run-tag>`
- Run the outer loop once against the board and assert it emits a run for task1.
- Run real inner-loop/Codex execution by using Loops inner-loop command in config (no stub command).

### Technology Stack
- Python for integration harness (`pytest`).
- GitHub CLI (`gh`) for GraphQL calls and auth reuse.
- Loops CLI (`loops`) for runtime invocation.
- Codex CLI for real agent execution.

### Integration Points
- GitHub Projects v2 GraphQL API (project items, field updates, labels, issue lifecycle).
- Loops CLI (`loops run --run-once`) with test-specific `.loops/config.json`.
- Local filesystem `.loops/` state, especially `.loops/jobs/*/run.json`, `run.log`, and `agent.log`, for assertions.

### Design Patterns
- Fixture-based setup/teardown for creating and cleaning project items/issues.
- Deterministic task isolation with provider filters and unique run labels.
- Explicit opt-in environment variable to run live tests.
- Idempotent cleanup to avoid board pollution.

### Important Context
- Provider poll order is oldest-first after filter application; tests should avoid relying on global board ordering.
- Outer loop creates run dirs under `.loops/jobs/` and writes `run.json` when it emits tasks.
- Outer loop materializes `run.json`, `run.log`, `agent.log`, and `inner_loop_runtime_config.json` before launch.
- `loops run --run-once` is the canonical command for one cycle.
- For clarity and control, the integration config should set an explicit `inner_loop.command` that executes Loops inner loop (which in turn invokes Codex).

---

## Steps

### Phase 1: Discovery & Access
- [x] Confirm GitHub auth (GITHUB_TOKEN/GH_TOKEN) and required scopes for Projects v2 mutation.
- [x] Query the project for field IDs and status options (`Status` field includes `Todo`).
- [x] Select a non-ready status for task2 to ensure only task1 is eligible (`Backlog`/`In Progress` fallback).

### Phase 2: Repo & Scaffolding
- [x] Reuse target repo `kevinslin/loops-integ`.
- [x] Add `tests/integ/` directory in Loops repo with helper modules.
- [x] Add shared helper for deterministic run label generation and integration env validation.

### Phase 3: Integration Harness Implementation
- [x] Implement `tests/integ/github_setup.py` to:
  - Create two issues in `kevinslin/loops-integ`.
  - Label both issues with a unique per-run tag.
  - Add both issues as project items to the target board.
  - Set task1 to `Todo` and task2 to a non-ready status.
  - Return identifiers needed for teardown (issue numbers, project item ids, run label).
- [x] Implement `tests/integ/test_outer_loop_pickup_live.py` (pytest) to:
  - Build test `.loops/config.json` pointing to the hardcoded project URL.
  - Set `provider_config.filters` for repository + unique run label.
  - Configure explicit inner-loop command for real Loops inner-loop/Codex execution.
  - Run `loops run --run-once --config .loops/config.json`.
  - Assert a run dir is created under `.loops/jobs/` and `run.json.task.title` matches task1.
  - Assert Codex execution is observable from run artifacts (for example `agent.log` / `run.log` content).
- [x] Add teardown/cleanup to remove project items and close issues even on failure.
- [x] Gate live tests behind an environment variable (for example `LOOPS_INTEG_LIVE=1`).

### Phase 4: Verification & Docs
- [x] Document how to run the live integration test in `README.md`.
- [x] Run the integration test and verify no leftover project items/issues.
- [x] Add a Makefile target (`make test-integ-live`) and runtime notes for opt-in execution.

**Dependencies between phases:**
- Phase 2 depends on Phase 1 for field/status discovery.
- Phase 3 depends on Phase 2 scaffolding.
- Phase 4 depends on Phase 3 implementation.

---

## Testing

- `LOOPS_INTEG_LIVE=1 python -m pytest tests/integ -k outer_loop_pickup_live -s`
- Optional manual sanity: `loops run --run-once --config .loops/config.json`

---

## Dependencies

### External Services/APIs
- GitHub Projects v2 GraphQL API: create issues, add items, update status fields.

### Libraries/Packages
- `pytest` for integration test execution.

### Tools/Infrastructure
- GitHub CLI (`gh`) for GraphQL calls and auth reuse.
- Loops CLI (`loops`) from editable install.
- Codex CLI for live inner-loop execution.
- Python 3.x environment.

### Access Required
- [x] GitHub token with repo + Projects v2 write access for the target project.
- [x] `gh` authenticated for the target account.
- [x] Codex authenticated in the local environment.

---

## Risks & Mitigations

| Risk | Impact | Probability | Mitigation Strategy |
|------|--------|-------------|---------------------|
| Project API access or scopes missing | High | Med | Validate token scopes in Phase 1; fail fast with actionable error. |
| Deterministic pickup fails on shared board | High | Med | Isolate test tasks via repository + unique run-label filters. |
| Test leaves residual issues/items | Med | Med | Ensure teardown runs in fixture finalizers and handles partial failures. |
| Live Codex run is slow/flaky or blocks CI | High | Med | Keep test opt-in; use bounded prompts and explicit timeout handling around test runs. |
| API rate limits/flaky network | Med | Low | Gate tests with opt-in env var; retry minimal API calls. |

---

## Questions

### Technical Decisions Needed
- [x] Should we use issues in `kevinslin/loops-integ` or draft items for the two tasks?
  - Answer: Use issues in `kevinslin/loops-integ`.
- [x] Is the status field name `Status` and does it include a `Todo` option on this board?
  - Answer: Yes.
- [x] Should this integration test invoke real Codex?
  - Answer: Yes (no inner-loop stub).

### Clarifications Required
- [x] Should the integration test be CI-ready, or is manual invocation sufficient?
  - Answer: Both (CI-ready but opt-in).

### Research Tasks
- [x] Capture project field IDs and status options via GraphQL for the board at `https://github.com/users/kevinslin/projects/6`.

---

## Success Criteria

- [x] Target repo `kevinslin/loops-integ` is used for live issue creation.
- [x] `tests/integ/` in the Loops repo contains a live integration test that uses real GitHub API calls.
- [x] Test creates two tasks, isolates them with provider filters, and confirms outer loop picks up task1 only.
- [x] Test observes Codex execution from Loops run artifacts.
- [x] Cleanup removes or closes created issues/items.
- [x] Documentation describes how to run the test (manual + CI opt-in).

---

## Notes

- Keep project board URL and `kevinslin/loops-integ` target hardcoded for now.
- Simplification: use `gh api graphql` from Python (via subprocess) instead of adding a new HTTP client dependency.
- Determinism relies on provider filters rather than global project ordering.
- Runtime control: set `CODEX_CMD` in the live test config to execute a fast real Codex command, and retry outer-loop polling to tolerate short GitHub Projects indexing delay.

## Changelog

- 2026-02-28: Implemented live integration harness under `tests/integ/`, added README/Makefile usage docs, and verified live run locally. (019ca771-d6ce-7712-b315-a12f5d46eb4b)
