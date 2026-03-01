# Feature Validation: Deterministic Pytest Import Context Across Worktrees

## Purpose

This is a validation spec, used to list post-testing validation that must be performed
by the user to confirm the feature implementation and testing is adequate

It should be updated during the development process, then kept as a record for later
context once implementation is complete.

**Feature Plan:** [2026-03-01-deterministic-test-import-context.md](/Users/kevinlin/.worktrees/loops/dev/deterministic-test-import-context/docs/specs/active/2026-03-01-deterministic-test-import-context.md)

**Implementation Plan:** [2026-03-01-deterministic-test-import-context.md](/Users/kevinlin/.worktrees/loops/dev/deterministic-test-import-context/docs/specs/active/2026-03-01-deterministic-test-import-context.md)

## Stage 4: Validation Stage

> AGENT INSTRUCTIONS:
> 
> Review all implementation and testing done to date and fill in the sections below with
> automated validation that has been done and remaining manual validatin needed.

## Validation Planning

- Focus on deterministic import resolution under pytest across local multi-worktree setups.

## Automated Validation (Testing Performed)

> Describe the testing already performed and any additional testing needed to validate
> this feature is working end to end and reviewable by the user.

### Unit Testing

- `python -m pytest tests/test_test_import_context.py -q` (`2 passed`)

### Integration and End-to-End Testing

- `python -m pytest tests/test_outer_loop.py tests/test_inner_loop.py -q` (`118 passed`)
- `python -m pytest -q` (`197 passed, 1 skipped`)

### Manual Testing Needed

> Describe the steps the user should take to validate this feature is working as
> expected.
> 
> Give a detailed list of manual validation steps the user must perform to confirm the
> all code and features implemented in these specs.
> 
> Do NOT include tests that are already automated and included and have been validated
> as part of the implementation plan.
> 
> Include all aspects of workflows that the user should test, or aspects that may be new
> to the user and they should see to be completely current on the system:
> 
> - Any new backend workflows that need a sanity check or manual inspection
>
> - Exact CLI commands that the user should validate and also confirm the output and
>   styling are correct
>
> - Sanity checking database state or file state, especially if the user has not seen
>   these
>
> - All visual or UX changes to any web or GUI interfaces.
>
> - The most common workflows involving these UX changes
> 
> When done:
> 
> - Ask the user to do a full post-implementation review, including the acceptance
>   testing above.
>
> - Ask for any further updates or revisions needed.
>
> - Add all feedback and requests for revisions below.
>
> - Add new Phase above and revise the implementation if necessary.

- Manual check plan:
  - From this worktree, run `python -m pytest tests/test_outer_loop.py -q`.
  - Confirm command passes without setting `PYTHONPATH` manually.
  - Confirm no evidence of imports resolving to another checkout path.

## Manual Notes 

[keep this for the user to add notes. do not change between edits]

## Changelog
- 2026-03-01: Created validation spec for deterministic pytest import-path behavior. (019caa54-4d1b-7712-9f8c-de8271aa0e30)
- 2026-03-01: Recorded executed validation results for deterministic import-path guard implementation. (019caa54-4d1b-7712-9f8c-de8271aa0e30)
- 2026-03-01: Updated validation results after canonical-path dedupe hardening. (019caa54-4d1b-7712-9f8c-de8271aa0e30)
