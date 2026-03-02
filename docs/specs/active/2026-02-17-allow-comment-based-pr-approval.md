# Execution Plan: Allow comment-based PR approval from allowed usernames

**Date:** 2026-02-17
**Status:** Complete

---

## Goal

Extend inner-loop review polling so a PR is treated as approved when either:

1. GitHub `reviewDecision` is approved (current behavior), or
2. A qualifying approval comment from an allowlisted username indicates approval.

This allows teams using comment-driven approval workflows to unblock cleanup/finish flow without requiring a formal GitHub review approval.

---

## Context

### Background

Inner-loop currently advances to `PR_APPROVED` only when GitHub review decision resolves to approved. Some review workflows use privileged comments (human or bot) to indicate approval, which Loops currently ignores.

### Current State

- `loops/inner_loop.py` in `WAITING_ON_REVIEW` calls `_fetch_pr_status_with_gh(...)`.
- `_fetch_pr_status_with_gh(...)` currently requests:
  - `reviewDecision,mergedAt,url,number,repository,latestReviews`
- Review status is derived only by `_review_status_from_decision(...)`.
- `derive_run_state(...)` transitions to `PR_APPROVED` only when `pr.review_status == "approved"`.
- There is no allowlist for comment-based approval actors.

### Temporal Context Check (Required)

| Value | Source of Truth | Representation | Initialization / Capture | First Consumer | Initialized Before Consumption |
| --- | --- | --- | --- | --- | --- |
| Allowed approval usernames | `OuterLoopConfig` | `tuple[str, ...]` (normalized usernames) | Parsed in `load_config`, propagated to inner-loop launch context | comment-approval matcher | Yes |
| `reviewDecision` | `gh pr view --json` payload | string (`APPROVED`, `CHANGES_REQUESTED`, etc.) | captured per poll in `_fetch_pr_status_with_gh` | review-status derivation | Yes |
| Latest changes-requested review timestamp | `latestReviews` from payload | ISO timestamp string or `None` | extracted during poll response mapping | comment override guard | Yes |
| Latest qualifying approval comment timestamp | `comments` from payload | ISO timestamp string or `None` | extracted during poll response mapping | comment override guard | Yes |
| Final `review_status` | Computed in `_fetch_pr_status_with_gh` | `"open" | "changes_requested" | "approved"` | after decision + comment evaluation | `derive_run_state` | Yes |

### Constraints

- Preserve existing behavior when no allowlist is configured.
- Keep single-writer ownership of `run.json` in inner loop.
- Avoid trusting comments from non-allowlisted users.
- Avoid stale comment approvals overriding newer `changes_requested` reviews.
- Keep dependencies unchanged (no new package requirements).

---

## Technical Approach

### Architecture / Design

Add a comment-based approval override path inside `_fetch_pr_status_with_gh(...)`:

1. Compute base review status from `reviewDecision` (existing).
2. If base status is not approved, evaluate approval comments:
   - author is in configured allowlist
   - comment body matches approval signal pattern
   - comment timestamp is newer than latest `CHANGES_REQUESTED` review timestamp (if present)
3. If qualifying comment exists, set `review_status="approved"` for this poll result.

### Configuration Surface

Add explicit fields to `GithubProjectsV2TaskProviderConfig`:

- `approval_comment_usernames`:
  - list/tuple of GitHub usernames
  - case-insensitive matching after normalization
  - empty means feature disabled (current behavior)

- `approval_comment_pattern`:
  - regex used to identify an approval comment body
  - default: anchored explicit approval command (for example `^\s*/approve\b`)
  - invalid pattern should log warning and fall back to default

### Data Flow Changes

- Update `gh pr view` JSON fields to include comments.
- Add helpers in `loops/inner_loop.py`:
  - parse/normalize allowed usernames
  - extract latest review timestamp by state (reuse for `CHANGES_REQUESTED`)
  - extract latest qualifying approval comment timestamp
  - compute final review status with override guard
- Update run materialization in `loops/outer_loop.py` so each run includes deterministic approval settings from provider config (without requiring user-set env vars).

### Integration Points

- `loops/outer_loop.py`:
  - Provider config schema + config loading
  - launcher propagation of approval settings
- `loops/inner_loop.py`:
  - `_fetch_pr_status_with_gh`
  - review status extraction helpers
  - run log message when comment-based approval override is applied
- `README.md`:
  - document new `task_provider_config` fields
- `DESIGN.md`:
  - note alternate approval signal path (review decision OR allowlisted approval comment)

### Design Patterns

- Conservative override:
  - only allow explicit actors
  - only allow explicit approval signals
  - require ordering guard against newer `changes_requested`
- Fail-safe defaults:
  - disabled unless configured
  - invalid config falls back safely without crashing run loop

### Important Context

- No `RunPR` schema change is required in this phase; override affects computed `review_status` only.
- Existing `review_addressed_at`/`latest_review_submitted_at` semantics remain review-event-focused.

---

## Steps

### Phase 1: Comment approval signal extraction
- [x] Add helper to parse allowlisted usernames from `OuterLoopConfig`-derived launch settings.
- [x] Add helper to evaluate comment body against approval regex.
- [x] Add helper to extract latest qualifying approval comment timestamp from `comments`.

### Phase 2: Review status derivation changes
- [x] Expand `_fetch_pr_status_with_gh` JSON fields to include comments.
- [x] Compute latest `CHANGES_REQUESTED` review timestamp.
- [x] Apply approval override when qualifying comment is newer than latest changes-requested signal.
- [x] Emit run-log entry when comment override is used.

### Phase 3: Documentation and hardening
- [x] Update `README.md` with `task_provider_config` fields and examples.
- [x] Update `DESIGN.md` approval path description.
- [x] Add clear error/log behavior for invalid regex configuration.

### Phase Dependencies

- Phase 2 depends on Phase 1 helper functions.
- Phase 3 depends on finalized behavior from Phase 2.

---

## Testing

Integration tests:
- [x] Inner loop transitions to `PR_APPROVED` when `reviewDecision` is open but qualifying allowlisted approval comment exists.
- [x] Inner loop remains `WAITING_ON_REVIEW` when approval comment is from non-allowlisted user.
- [x] Inner loop remains `WAITING_ON_REVIEW` when approval comment is older than latest `changes_requested` review.

Unit tests:
- [x] Username normalization and matching (case-insensitive, trimming, dedupe).
- [x] Comment approval matcher behavior with default regex.
- [x] Invalid custom regex falls back to default behavior safely.
- [x] Timestamp ordering helper for `changes_requested` vs approval comment.

Manual validation:
- [x] Configure `task_provider_config.approval_comment_usernames`.
- [x] Simulate a PR with no approved review decision but allowlisted approval comment.
- [x] Confirm `run.log` records comment-based approval detection and loop enters cleanup path.

---

## Dependencies

### External Services or APIs
- GitHub PR API via `gh pr view --json`: source for `reviewDecision`, `latestReviews`, and `comments`.

### Libraries or Packages
- None new (reuse stdlib + existing tooling).

### Tools or Infrastructure
- `gh` CLI: required for PR polling.

### Access Required
- [x] GitHub token with read access to PR metadata/comments.

---

## Risks and Mitigations

| Risk | Impact | Probability | Mitigation |
| --- | --- | --- | --- |
| False-positive approval from ambiguous comment text | High | Med | Use explicit default regex and allowlist-only matching. |
| Stale approval comment overrides newer requested changes | High | Med | Require approval comment timestamp to be newer than latest `CHANGES_REQUESTED` review timestamp. |
| `gh` comment payload shape differs/missing | Med | Low | Defensive parsing; fallback to current reviewDecision-only behavior. |
| Misconfigured allowlist causes no-op behavior | Low | Med | Log resolved allowlist count and document env format clearly. |

---

## Questions

### Technical Decisions Needed
- [x] Confirm default approval comment pattern (recommended: explicit `/approve` command).
  - Chosen: strict default `^\s*/approve\b`.
- [x] Decide whether to allow multiple approval patterns out of the box (for example `approved`, `lgtm`) or keep strict default and rely on custom regex.
  - Chosen: keep strict default; custom patterns via `approval_comment_pattern`.

### Clarifications Required
- [x] Should approval comments from bot accounts be allowed by default, or only when explicitly listed in allowlist?
  - Chosen: only when explicitly listed in allowlist.

### Research Tasks
- [x] Verify exact `gh pr view --json comments` payload fields used in this repo environment for robust parser assumptions.

---

## Success Criteria

- [x] PRs with allowlisted approval comments can transition to `PR_APPROVED` without GitHub reviewDecision=APPROVED.
- [x] Non-allowlisted or stale comments do not trigger approval.
- [x] Existing behavior is unchanged when allowlist is unset.
- [x] Tests and docs are updated and passing.

---

## Notes

- Simplification: keep user-facing configuration in provider config; inner-loop reads run-scoped approval config materialized by outer loop.
- Simplification: keep `RunPR` schema unchanged; only review-status computation is extended.

## Manual Notes 

[keep this for the user to add notes. do not change between edits]

## Changelog
- 2026-03-02: Moved approval comment settings from `loop_config` to GitHub provider config and updated doctor/config docs accordingly. (019cabd8-6116-7542-aead-8d1fd6d6b985)
- 2026-03-01: Added deterministic best-effort 👍 reactions when an allowlisted plain PR comment is the winning approval signal in review polling. (019cab4c-0485-7542-b9eb-ff1c83ca0942)
- 2026-02-17: Created initial feature spec for allowlisted comment-based PR approval override. (019c68ed-a6c5-78e0-891a-6b70a1a1450c)
- 2026-02-17: Implemented `OuterLoopConfig`-driven comment approval override, tests, and docs updates. (019c68ed-a6c5-78e0-891a-6b70a1a1450c)
