# Feature Spec: End-to-End Integration Test Target

**Date:** 2026-03-01
**Status:** Planning

---

## Goal and Scope

### Goal
Add an explicit opt-in end-to-end integration target, `make test-integ-end2end`, that validates a full live Loops task lifecycle against `kevinslin/loops-integ`: task pickup, PR creation, approval-to-merge path, and post-run cleanup.

### In Scope
- Add new Makefile target: `test-integ-end2end`.
- Add a live integration harness flow that:
  - bootstraps `.integ/loops-integ` (clone-or-pull + `loops init`),
  - creates one deterministic “make animal” task on the integration board,
  - runs `loops run --run-once` with a 15-minute timeout,
  - asserts full-lifecycle outcomes (run dir, completed task status, approved PR, merged PR),
  - performs teardown (revert merged change, push revert, archive jobs).
- Add a first-class `loops clean` command to archive `.loops/jobs/*` after the run.
- Document explicit invocation and prerequisites (manual-only; never part of default test suite).

### Out of Scope
- Running this live end-to-end target in default `make test`.
- Replacing current live harness test (`outer_loop_pickup_live`).
- Building generalized multi-repo orchestration.

---

## Context and Constraints

### Background
The existing live integration harness proves deterministic task pickup and Codex invocation, but it intentionally stops before a full merged lifecycle and does not verify board completion semantics or teardown hygiene in the target repo.

### Current State
- Existing opt-in live harness:
  - `tests/integ/test_outer_loop_pickup_live.py`
  - `tests/integ/github_setup.py`
  - `make test-integ-live`
- No `loops clean` CLI command currently exists.
- Current harness asserts `needs_user_input` after Codex turn; it does not assert merged PR outcomes.

### Required Pre-Read
- `DESIGN.md`
- `docs/flows/ref.outer-loop.md`
- `docs/flows/ref.inner-loop.md`
- `docs/flows/ref.integration-test-flow.md`
- `docs/specs/.archive/2026-02-09-create-integration-testing-harness-for-loops.md`
- `tests/integ/test_outer_loop_pickup_live.py`
- `tests/integ/github_setup.py`
- `Makefile`
- `/Users/kevinlin/kevin-garden/kevin-private/notes/task.2026.03.01.end2end-integ-test.md`

### Constraints
- Must remain explicit opt-in only.
- Must run against live GitHub + live Codex execution.
- Must hard timeout at 15 minutes for `loops run --run-once`.
- Must keep cleanup idempotent and safe when setup or assertions fail mid-run.
- Must keep implementation simple by reusing existing integration harness helpers.

### Temporal Context Check (Required)

| Value | Source of Truth | Representation | Initialization / Capture | First Consumer | Initialized Before Consumption |
| --- | --- | --- | --- | --- | --- |
| Integration run label | `build_run_label()` in `tests/integ/github_setup.py` | string label (`loops-integ-...`) | setup before issue creation | provider filter in config (`tag=<run_label>`) | Yes |
| Run lifecycle state | `.loops/jobs/<run>/run.json` | `last_state`, `pr`, `auto_approve` fields | written by outer loop + inner loop | end-to-end assertions | Yes |
| Project status completion option | project `Status` field options via GraphQL | option id (`Done`/`Completed`) | setup metadata fetch | completion assertion after run | Yes |

Gate answer:
- No ordering violation identified. Plan keeps setup metadata discovery before lifecycle assertions.

### Non-obvious Dependencies or Access (Optional)
- Push access to `kevinslin/loops-integ` is required for both feature branch merge and teardown revert commit.
- GitHub token must support Projects V2 reads/writes and PR/issue mutation.
- Auto-approve path depends on PR status/CI signals reaching an approvable state in the test window.

---

## Approach and Touchpoints

### Proposed Approach
Extend the current live harness with one additional end-to-end test path and one cleanup CLI command:
1. Reuse existing GitHub setup helpers for deterministic task provisioning.
2. Add minimal `.integ/loops-integ` bootstrap logic directly in the new end-to-end test module (no extra framework module).
3. Execute one full live run and assert terminal lifecycle outcomes from run artifacts and GitHub state.
4. Add `loops clean` to archive jobs and use it in teardown.
5. Keep approval and completion assertions deterministic by explicitly validating:
   - `run.json.auto_approve.verdict == "APPROVE"`,
   - merged PR metadata exists (`pr.merged_at`),
   - project item status equals discovered done-option id.

### Integration Points / Touchpoints
- `Makefile` (`test-integ-end2end` target).
- `tests/integ/github_setup.py` (task provisioning and project status checks).
- New test module: `tests/integ/test_end2end_live.py`.
- `loops/cli.py` (add `clean` command).
- `loops/outer_loop.py` (shared loops-root and jobs directory constants reused by clean command).
- `README.md` (manual invocation docs).
- `DESIGN.md` (CLI/storage contract update for archived jobs).

### Resolved Ambiguities / Decisions
- Keep this as a standalone target (`make test-integ-end2end`), separate from existing live pickup test.
- Keep one scenario only (single “make animal” task) to minimize moving parts.
- Use deterministic naming (`make animal (<run-label>)`) and unique run label for isolation.
- Add `loops clean` as the canonical archive mechanism rather than test-only ad-hoc deletion logic.
- Resolve done-status assertion dynamically from board metadata (`Done` preferred, fallback `Completed`) and fail fast if neither exists.
- Require explicit auto-approve evidence in `run.json` rather than inferring from final merge state alone.

### Important Implementation Notes (Optional)
- The test should assert both local run artifacts (`run.json`, `run.log`) and remote GitHub state (project item status + PR merged state).
- Teardown should always run in `finally` and surface cleanup errors after primary assertion failures.

---

## Phases and Dependencies

### Phase 1: Bootstrap and Command Surfaces
- [ ] Add `make test-integ-end2end` target (explicit opt-in only).
- [ ] Add `.integ/loops-integ` bootstrap steps inside `tests/integ/test_end2end_live.py`:
  - clone if missing,
  - fetch + reset to latest default branch if present,
  - run `loops init` (idempotent).
- [ ] Add `loops clean` CLI command with MVP contract:
  - default target: `.loops/jobs/*` under resolved loops root,
  - behavior: move run dirs into `.loops/archive/<timestamp>/`,
  - options: `--loops-root`, `--dry-run`,
  - output: archived run count + destination path.

### Phase 2: End-to-End Task Provisioning
- [ ] Extend integration setup helpers to create one “make animal” issue with deterministic task body (ASCII-art pun request).
- [ ] Ensure project item status is initially ready (`Todo`) and tagged for deterministic filtering.
- [ ] Resolve and store completion option id (`Done` or `Completed`) during project metadata discovery for later assertion.
- [ ] Write run config under the integration workspace with `sync_mode=true` and timeout controls.

### Phase 3: Live Run and Assertions
- [ ] Execute `loops run --run-once` and enforce 15-minute timeout.
- [ ] Assert local artifacts:
  - exactly one run dir created,
  - `run.json` exists and contains expected task,
  - PR is present and merged (`pr.url`, `pr.merged_at`),
  - `auto_approve.verdict == "APPROVE"`,
  - run reached terminal completion (`last_state == DONE`).
- [ ] Assert remote outcomes:
  - project item moved to resolved completion option id,
  - PR approved path satisfied via run-record evidence,
  - PR merged in target repo.

### Phase 4: Teardown and Hygiene
- [ ] Revert merged change in `.integ/loops-integ` and push revert commit.
- [ ] Run `loops clean` to archive generated jobs.
- [ ] Close/delete integration artifacts as needed (issues/items) while preserving failure diagnostics.

### Phase Dependencies
- Phase 2 depends on Phase 1 command/bootstrap availability.
- Phase 3 depends on Phase 2 deterministic setup.
- Phase 4 depends on Phase 3 run outputs but must still execute on Phase 3 failure.

---

## Validation and Done Criteria

### Validation Plan

Integration tests:
- `make test-integ-end2end`
- `LOOPS_INTEG_END2END=1 python -m pytest tests/integ -k end2end_live -s`

Unit tests:
- `python -m pytest tests/test_cli.py -k clean`
- `python -m pytest tests/test_integ_github_setup.py -k end2end`

Manual validation:
- Confirm target is absent from default `make test` execution path.
- Confirm `.loops/archive/` receives archived run directories after `loops clean`.
- Confirm target repo branch is restored via teardown revert commit.

### Done Criteria
- [ ] `make test-integ-end2end` exists and is not referenced by default test target.
- [ ] End-to-end run creates a run dir and finishes with merged PR evidence.
- [ ] Integration task is observed in completed status after run.
- [ ] Approval-to-merge path is asserted from run record/GitHub state.
- [ ] Teardown reverts merged change, pushes revert, and archives jobs via `loops clean`.
- [ ] README and DESIGN updates document invocation and cleanup behavior.

---

## Open Items and Risks

### Open Items
- [ ] Confirm whether teardown should keep or close the original task issue after revert commit (depends on board hygiene preference).

### Risks and Mitigations

| Risk | Impact | Probability | Mitigation |
| --- | --- | --- | --- |
| Live Codex/GitHub latency exceeds 15-minute timeout | High | Med | Keep scenario minimal, enforce deterministic task prompt, and emit actionable timeout diagnostics. |
| PR does not reach auto-approvable state (CI/review gate mismatch) | High | Med | Make approval gating explicit in test setup and fail with detailed run/pr status snapshots. |
| Teardown revert fails and leaves target repo dirty | High | Low | Always-run teardown with explicit git-state checks and surfaced cleanup errors. |
| `loops clean` archives unintended directories | Med | Low | Scope strictly to `.loops/jobs/*`, add dry-run/testing coverage, and keep archive path deterministic. |

### Simplifications and Assumptions (Optional)
- Reuse current `tests/integ` helper stack rather than introducing a separate framework.
- Start with a single deterministic end-to-end scenario before adding matrixed variants.
- Keep this target manual-only to prevent CI flakiness and accidental repo mutations.

---

## Outputs

- PR created from this spec: Not started.

## Manual Notes 

[keep this for the user to add notes. do not change between edits]

## Changelog
- 2026-03-01: Created feature spec for `test-integ-end2end`, including setup/run/assert/teardown flow and `loops clean` command plan. (019cabfd-e874-7dc1-a382-463cf0980fbe)
