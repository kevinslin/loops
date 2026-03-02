# Feature Spec: Deterministic PR Discovery

**Date:** 2026-03-01
**Status:** Completed

---

## Goal and Scope

### Goal
Replace best-effort PR URL extraction from Codex stdout with a deterministic PR discovery path for the initial PR push, so the inner loop can reliably populate `run.json` and transition states without regex-only heuristics.

### In Scope
- Add a standalone stdlib-only script at `scripts/push-pr.py` that:
  - accepts `<pr-title>` and `<pr-body-file>` args
  - writes the initial PR body template to `<pr-body-file>`
  - resolves base branch via `gh repo view` with `git symbolic-ref refs/remotes/origin/HEAD` fallback
  - creates a PR via `gh pr create --base ... --title ... --body-file ...`
  - writes PR URL deterministically to `${LOOPS_RUN_DIR}/push-pr.url` for inner-loop consumption
- Replace current `trigger:push-pr` instructions wherever referenced with:
  1. if unstaged changes exist -> `trigger:commit-code`
  2. invoke standalone `push-pr.py <pr-title> <pr-body-file>`
  3. invoke `trigger:check-ci`; on failure invoke `trigger:fix-pr`
- Inner-loop gating for PR discovery so deterministic PR acquisition runs only when:
  - current state is `RUNNING`
  - Codex session exits cleanly (`exit_code == 0`)
  - turn does not request user input
- Missing PR URL handling:
  - force `NEEDS_INPUT`
  - persist a clear `needs_user_input_payload`
  - log a visible notification that PR URL discovery failed
- Plan how `push-pr.py` is made reachable from the inner-loop execution context.

### Out of Scope
- Redesigning review/merge state machine behavior outside PR URL discovery.
- Changing provider polling semantics.
- Adding non-stdlib Python dependencies.

---

## Context and Constraints

### Background
Current PR discovery is partly heuristic. `_run_codex_turn(...)` currently calls `_extract_pr_from_output(...)`, which scans JSON lines and then falls back to regex matching in raw output. This is fragile when agent output format changes or when the PR URL is written to files rather than stdout.

### Current State
- `loops/inner_loop.py`:
  - `_run_codex_turn(...)` discovers PRs via `_extract_pr_from_output(output)`.
  - if `exit_code == 0` and no PR is detected and no explicit `NEEDS_INPUT`, it forces `needs_user_input`.
- `/Users/kevinlin/.codex/skills/dev.shortcuts/references/shortcuts/push-pr.md` currently uses inline shell logic and writes a PR URL file in `/tmp/...-devloop-pr`, but inner-loop PR discovery still depends on parsing Codex stdout.
- No deterministic contract currently ties the initial PR push step to run-scoped PR discovery in `run.json`.

### Required Pre-Read (LLM Agent)
- `DESIGN.md` (state machine + trigger contracts)
- `docs/specs/active/2026-02-09-manage-inner-loop-state-machine.md`
- `loops/inner_loop.py` (`_run_codex_turn`, `_extract_pr_from_output`, state/needs-input handling)
- `loops/run_record.py` (`RunRecord`, `derive_run_state`)
- `tests/test_inner_loop.py` (turn/state behavior expectations)
- `/Users/kevinlin/.codex/skills/dev.shortcuts/references/shortcuts/push-pr.md`

### Constraints
- `push-pr.py` must live in `/scripts` and use only Python standard library.
- PR discovery for initial push must only run in `RUNNING` with clean Codex exit and no user-input request.
- If no PR URL can be found, loop must require user input and emit a clear notification/log.
- Inner-loop may run with custom working directories, so script path resolution must not assume current working directory equals project root.

### Non-obvious Dependencies or Access
- `gh` CLI auth and repo access are required for `gh pr create` and default branch lookup.
- Shortcut source-of-truth is outside this repo (`$CODEX_HOME/skills/dev.shortcuts/...`), so migration may require coordinated update if the runtime environment does not vendor these files from repository state.

---

## Approach and Touchpoints

### Proposed Approach
Introduce a deterministic PR artifact contract for initial PR creation:
- `push-pr.py` handles PR body generation + PR creation and writes the resulting PR URL to `${LOOPS_RUN_DIR}/push-pr.url`.
- `_run_codex_turn(...)` reads that artifact only for eligible `RUNNING` turns with clean exit, no `NEEDS_INPUT` request, and no existing `run.json.pr`.
- If artifact is absent/invalid, inner loop transitions to `NEEDS_INPUT` with explicit operator guidance; a later turn can recover when handoff user input includes a valid PR URL.

### Integration Points / Touchpoints
- `scripts/push-pr.py` (new)
- `loops/inner_loop.py`
  - add deterministic PR discovery helper(s)
  - gate invocation by state + clean exit + no-user-input conditions
  - missing-PR escalation payload + notification log
- `tests/test_inner_loop.py`
  - add deterministic discovery and gating coverage
- `DESIGN.md`
  - document deterministic PR discovery contract and failure-to-discover escalation path
- `/Users/kevinlin/.codex/skills/dev.shortcuts/references/shortcuts/push-pr.md`
  - replace inline shell PR creation with `push-pr.py` invocation
- Any references still using deprecated trigger names:
  - `trigger:merge-pr-basic` -> `trigger:sync-branch`

### Resolved Ambiguities / Decisions
- Deterministic PR discovery is tied to initial PR push turns, not review-feedback turns.
- Missing PR URL is a blocking operator event (`NEEDS_INPUT`), not a silent retry.
- `push-pr.py` owns PR body template creation and PR creation workflow in one place.
- Script must be runnable without third-party Python packages.

### Important Implementation Notes
- Gating rule implementation should use the effective loop state entering `_run_codex_turn(...)` plus post-turn signals (`exit_code`, trailing state marker) to avoid false positives.
- Run-scoped PR artifact file should be cleared/overwritten deterministically per initial push attempt to avoid stale URL reuse.
- Inner-loop prompt contract for initial PR creation requires direct invocation of `push-pr.py` during `RUNNING` turns (not a trigger indirection). `loops/inner_loop.py` does not invoke the script directly.
- Initial PR command sequence must invoke the script with an absolute path derived from `LOOPS_RUN_DIR`:
  ```bash
  REPO_ROOT="$(git -C "${LOOPS_RUN_DIR:?LOOPS_RUN_DIR is required}" rev-parse --show-toplevel)"
  python3 "$REPO_ROOT/scripts/push-pr.py" "$PR_TITLE" "$PR_BODY_FILE"
  ```
- `push-pr.py` must fail fast with a clear error when `LOOPS_RUN_DIR` is missing, because `${LOOPS_RUN_DIR}/push-pr.url` is part of the deterministic discovery contract.
- `push-pr.py` should resolve repo root from `LOOPS_RUN_DIR` and run `git`/`gh` subprocesses with explicit repository `cwd`.

---

## Phases and Dependencies

### Phase 1: Script Contract (`push-pr.py`)
- [x] Add `scripts/push-pr.py` with stdlib-only argument parsing and subprocess usage.
- [x] Implement PR body-file write with this exact template:
  - `[feat|enhance|chore|fix|docs]: [description of change]`
  - `## Context`
  - `## Testing`
- [x] Implement base-branch discovery fallback chain and explicit error on failure.
- [x] Create PR via `gh pr create` and capture PR URL.
- [x] Write PR URL to `${LOOPS_RUN_DIR}/push-pr.url` (overwrite on each initial PR push attempt).

### Phase 2: Shortcut and Instruction Migration
- [x] Update `trigger:push-pr` instruction text to:
  1. unstaged changes -> `trigger:commit-code`
  2. resolve `REPO_ROOT` from `LOOPS_RUN_DIR` and call `python3 "$REPO_ROOT/scripts/push-pr.py" <pr-title> <pr-body-file>`
  3. `trigger:check-ci`, then `trigger:fix-pr` on failure
- [x] Replace deprecated trigger references as requested:
  - `trigger:merge-pr-basic` -> `trigger:sync-branch`
- [x] Audit for all remaining `trigger:push-pr` mentions and update to the new deterministic flow.

### Phase 3: Inner-Loop Deterministic Discovery + Escalation
- [x] Add deterministic PR artifact read path in `loops/inner_loop.py` for eligible initial push turns.
- [x] Enforce discovery gate: only when state=`RUNNING`, exit cleanly, and no user-input request.
- [x] On missing/invalid PR URL, force `NEEDS_INPUT` with explicit payload and run-log notification.
- [x] Remove `_extract_pr_from_output` PR fallback usage for the initial PR discovery path.
- [x] Update prompt/shortcut-facing docs so `trigger:push-pr` invocation details are explicit and consistent with the absolute-path contract.

### Phase 4: Tests and Docs
- [x] Add/adjust tests in `tests/test_inner_loop.py` for gating, deterministic read, and missing-URL escalation.
- [x] Update `DESIGN.md` to document deterministic PR discovery behavior and constraints.

### Phase Dependencies
- Phase 3 depends on Phase 1 artifact contract.
- Phase 2 must be complete before enabling strict artifact-only initial PR discovery.
- Phase 4 depends on behavior stabilization from Phases 1-3.

---

## Validation and Done Criteria

### Validation Plan

Integration tests:
- `RUNNING` + clean exit + no user-input marker + artifact PR URL => `run.json.pr.url` populated deterministically.
- `RUNNING` + clean exit + no user-input marker + missing artifact => `NEEDS_INPUT` with message/context indicating PR URL not found.
- `RUNNING` + clean exit + missing artifact + user handoff response that includes PR URL => `run.json.pr.url` recovered from user input without additional artifact writes.
- `RUNNING` + trailing `<state>NEEDS_INPUT</state>` => does not enforce PR discovery contract for that turn.
- review-feedback turn does not attempt initial deterministic PR discovery.

Unit tests:
- `push-pr.py` base-branch resolution fallback behavior.
- `push-pr.py` body-file generation behavior and argument validation.
- PR URL artifact parser validation (valid URL, malformed content, missing file).

Manual validation:
- Trigger initial PR push with `trigger:push-pr` and verify script invocation path.
- Confirm PR URL appears in deterministic artifact and then in `.loops/jobs/.../run.json`.
- Simulate missing artifact and confirm visible notification + handoff request.

### Done Criteria
- [x] `push-pr.py` exists under `/scripts` and uses only stdlib.
- [x] All `trigger:push-pr` instructions use the new 3-step deterministic flow.
- [x] `trigger:push-pr` explicitly resolves `REPO_ROOT` from `LOOPS_RUN_DIR` and invokes `python3 "$REPO_ROOT/scripts/push-pr.py" ...`.
- [x] Initial PR discovery is gated to `RUNNING` + clean exit + no user-input request.
- [x] Missing PR URL reliably escalates via `NEEDS_INPUT` and notification/log.
- [x] Tests and design docs updated and passing.

---

## Open Items and Risks

### Open Items
- [ ] None.

### Risks and Mitigations

| Risk | Impact | Probability | Mitigation |
| --- | --- | --- | --- |
| Script path unavailable from inner-loop working directory | High | Med | Inject stable script path via environment/runtime config and call via absolute path. |
| Stale PR artifact reused across turns | High | Med | Overwrite `${LOOPS_RUN_DIR}/push-pr.url` on each initial push attempt and validate URL matches GitHub PR pattern. |
| Mixed trigger names in shortcut references | Med | Med | Run explicit repo+skill audit for `trigger:push-pr` and `trigger:merge-pr-basic` before closing. |
| `gh` lookup/create failures produce ambiguous operator state | Med | Med | Emit explicit error text + force `NEEDS_INPUT` with actionable context. |

### Simplifications and Assumptions
- Deterministic discovery is scoped to initial PR creation, not merge/cleanup phases.
- Existing state derivation (`derive_run_state`) remains unchanged; only PR discovery inputs are hardened.

---

## Outputs

- PR created from this spec: https://github.com/kevinslin/loops/pull/70

## Manual Notes 

[keep this for the user to add notes. do not change between edits]

## Changelog
- 2026-03-01: Created feature spec for deterministic PR discovery, push-pr script contract, and trigger migration requirements. (019cab67-3061-7ce1-81c1-e30f80798fb0)
- 2026-03-01: Updated plan per review decisions: use `trigger:fix-pr`, remove fallback path, and pin artifact contract to `${LOOPS_RUN_DIR}/push-pr.url`. (019cab67-3061-7ce1-81c1-e30f80798fb0)
- 2026-03-01: Added concrete inner-loop invocation detail for `push-pr.py` (absolute path resolution from `LOOPS_RUN_DIR` via `trigger:push-pr`). (019cab67-3061-7ce1-81c1-e30f80798fb0)
- 2026-03-01: Implemented deterministic initial PR discovery (`push-pr.url`), added `scripts/push-pr.py`, updated shortcut instructions, and landed tests/docs updates. (019cab67-3061-7ce1-81c1-e30f80798fb0)
- 2026-03-01: Addressed review follow-ups: force active `LOOPS_RUN_DIR`, harden artifact read failures, add handoff PR URL recovery path, and run git/gh subprocesses from resolved repo root cwd. (019cab67-3061-7ce1-81c1-e30f80798fb0)
