# Feature Spec: Deterministic Pytest Import Context Across Worktrees

**Date:** 2026-03-01
**Status:** Completed

---

## Goal and Scope

### Goal
Ensure pytest always resolves the `loops` package from the active worktree so test runs are deterministic even when other local checkouts exist.

### In Scope
- Add a test-session import-path guard for pytest runs in this repo.
- Add regression coverage for the guard behavior.
- Update test-run documentation to reflect deterministic execution expectations.

### Out of Scope
- Changing runtime import behavior outside pytest.
- Refactoring external Codex skills or global shell configuration.

---

## Context and Constraints

### Background
Recent development sessions observed path bleed where pytest imported `loops` from a different local checkout (`/Users/kevinlin/code/loops`) instead of the active worktree. This can produce false failures or false passes.

### Current State
- `make test` runs `python -m pytest`.
- Ad hoc test commands still vary across sessions.
- There is no repo-level pytest guard that enforces local worktree import precedence.

### Required Pre-Read 
- [DESIGN.md](/Users/kevinlin/.worktrees/loops/dev/deterministic-test-import-context/DESIGN.md)
- [README.md](/Users/kevinlin/.worktrees/loops/dev/deterministic-test-import-context/README.md)
- [tests](/Users/kevinlin/.worktrees/loops/dev/deterministic-test-import-context/tests)

### Constraints
- Keep behavior simple and test-only.
- Preserve compatibility with existing pytest invocation patterns.
- Avoid introducing runtime dependencies.

---

## Approach and Touchpoints

### Proposed Approach
Add `tests/conftest.py` with a small helper that guarantees repository root is placed at index 0 of `sys.path` at test session startup.

### Integration Points / Touchpoints
- [tests/conftest.py](/Users/kevinlin/.worktrees/loops/dev/deterministic-test-import-context/tests/conftest.py): enforce import-path priority at pytest startup.
- [tests/test_test_import_context.py](/Users/kevinlin/.worktrees/loops/dev/deterministic-test-import-context/tests/test_test_import_context.py): regression coverage for path-priority helper.
- [README.md](/Users/kevinlin/.worktrees/loops/dev/deterministic-test-import-context/README.md): document deterministic test invocation expectations.

### Resolved Ambiguities / Decisions
- Decision: fix at pytest session layer (`tests/conftest.py`) rather than relying only on shell env.
- Decision: keep change test-scoped, not package-runtime scoped.

---

## Phases and Dependencies

### Phase 1: Add deterministic test import guard
- [x] Add `tests/conftest.py` path-priority helper and startup hook.
- [x] Ensure helper is minimal and side-effect scoped to pytest process.

### Phase 2: Add regression coverage
- [x] Add test verifying the helper reorders `sys.path` with repo root first.

### Phase 3: Docs + verification
- [x] Update README testing section with deterministic invocation guidance.
- [x] Run targeted tests, then full suite.

### Phase Dependencies
- Phase 2 depends on Phase 1 helper implementation.
- Phase 3 depends on phases 1 and 2 completing and tests passing.

---

## Validation and Done Criteria

### Validation Plan

Integration tests:
- `python -m pytest tests/test_outer_loop.py tests/test_inner_loop.py`
- `python -m pytest`

Manual validation:
- Run pytest in this worktree and confirm imports resolve to this worktree path.

### Done Criteria
- [x] Pytest import-path guard exists and is active.
- [x] Regression test exists and passes.
- [x] Full test suite passes.
- [x] Documentation reflects deterministic test guidance.

### Separate Validation Spec (Optional)
- [Feature Validation Spec](/Users/kevinlin/.worktrees/loops/dev/deterministic-test-import-context/docs/specs/validation-2026-03-01-deterministic-test-import-context.md)

---

## Open Items and Risks

### Open Items
- [ ] Confirm whether to additionally codify this in contributor workflow docs beyond README.

### Risks and Mitigations

| Risk | Impact | Probability | Mitigation |
| --- | --- | --- | --- |
| `sys.path` mutation causes unexpected test import side effects | Med | Low | Keep logic minimal and only place repo root at index 0. |
| Future contributors bypass documented commands | Med | Med | Keep guard in pytest startup so behavior remains deterministic regardless of invocation style. |

---

## Outputs

- PR created from this spec: pending

## Manual Notes 

[keep this for the user to add notes. do not change between edits]

## Changelog
- 2026-03-01: Created feature spec for deterministic pytest import context in worktrees. (019caa54-4d1b-7712-9f8c-de8271aa0e30)
- 2026-03-01: Implemented pytest path guard, regression test, README update, and passing verification runs. (019caa54-4d1b-7712-9f8c-de8271aa0e30)
- 2026-03-01: Hardened path dedupe to canonicalize aliases (symlink/trailing-slash variants) with added regression coverage. (019caa54-4d1b-7712-9f8c-de8271aa0e30)
