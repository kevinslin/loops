# Execution Plan: Add provider-side filters for GitHub Projects V2 (repository and tags)

**Date:** 2026-02-18
**Status:** Completed

---

## Goal

Add configurable provider-side filters to `github_projects_v2` so polling can include only project items matching `key=value` filters.

Initial supported filter keys:

1. `repository=<owner>/<repo>`
2. `tag=<label-name>` (repeatable for multiple tags)

This should reduce noisy task intake and allow targeting by repo/tag directly in `provider_config`.

---

## Context

### Background

Current provider behavior returns all mapped Issue/PR items from a GitHub Project V2 board. Downstream filtering in outer loop is status-based only (`task_ready_status`). There is no provider-level selector for repository or tags.

### Current State

- Provider config model: `GithubProjectsV2TaskProviderConfig` in `loops/providers/github_projects_v2.py` supports `url`, `status_field`, `page_size`, `github_token`.
- Provider polling maps items to `Task` via `_map_item_to_task(...)`.
- Mapped `Task` includes `repo`, but tag/label metadata is not requested.
- Provider limit behavior (`poll(limit=...)`) currently counts all mapped tasks toward `limit`.

### Temporal Context Check (Required)

| Value | Source of Truth | Representation | Initialization / Capture | First Consumer | Initialized Before Consumption |
| --- | --- | --- | --- | --- | --- |
| Raw filter expressions | `provider_config.filters` | `list[str]` of `key=value` | parsed during provider construction/config validation | filter parser/normalizer | Yes |
| Parsed filter state | provider runtime | structured filters (`repositories`, `tags`) | built from raw filters before `poll()` loop | item matcher in `poll()` | Yes |
| Repository identity | GraphQL `content.repository.nameWithOwner` | string | captured per item in poll response | repository filter matcher | Yes |
| Item tags | GraphQL `content.labels.nodes[].name` | list/tuple[str] | captured per item in poll response | tag filter matcher | Yes |
| Poll limit counter | provider loop state | integer remaining count | computed each iteration | early-return + page-size logic | **No (current logic for filtered mode)** |

Gate answer:
- `No` for limit counter in filtered mode with current implementation, because current counting is based on all mapped tasks. This spec includes a fix: count only matched tasks toward `limit`.

### Constraints

- Keep existing behavior unchanged when no filters are configured.
- Maintain strict config validation (`extra="forbid"`) and fail fast on malformed filters.
- Do not add external dependencies.
- Preserve provider pagination behavior and timeout/error semantics.

---

## Technical Approach

### Architecture / Design

Add a typed `filters` field to `GithubProjectsV2TaskProviderConfig` and implement provider-local parsing/matching.

Proposed config surface:

```json
{
  "provider_id": "github_projects_v2",
  "provider_config": {
    "url": "https://github.com/orgs/acme/projects/7",
    "status_field": "Status",
    "page_size": 50,
    "filters": [
      "repository=acme/repo-a",
      "tag=backend",
      "tag=priority/high"
    ]
  }
}
```

Filter semantics:

- `repository`:
  - case-insensitive exact match against `nameWithOwner`
  - repeatable; multiple repository filters are OR (`repository=a` OR `repository=b`)
- `tag`:
  - case-insensitive exact match against item label names
  - repeatable; multiple tag filters are AND (item must contain all configured tags)
- Combined keys use AND across filter groups:
  - `repository=acme/repo-a` + `tag=backend` means matching repo **and** matching tags.

### Technology Stack

- Python stdlib + existing Pydantic model validation.
- GitHub GraphQL via existing `gh api graphql` path.

### Integration Points

- `loops/providers/github_projects_v2.py`
  - extend config model
  - include labels in GraphQL query for Issue/PullRequest content
  - parse and apply filters during poll
  - adjust limit counting to matched tasks only
- `tests/test_github_projects_v2_provider.py`
  - add filter parser/matcher and pagination/limit behavior coverage
- `README.md` and `DESIGN.md`
  - document `provider_config.filters` format and semantics

### Design Patterns

- Parse once, match many: normalize filters once at provider init.
- Fail-fast validation: reject malformed/unsupported filter expressions before polling.
- Deterministic matching: explicit AND/OR semantics by key class.

### Important Context

- Tags for GitHub items map to Issue/PR labels (`labels.nodes[].name`).
- Filtering occurs provider-side before tasks are returned to outer loop.
- For filtered polls, `limit` applies to matched tasks, not raw project items.

---

## Steps

### Phase 1: Config and filter parser
- [x] Add `filters: list[str] = []` to `GithubProjectsV2TaskProviderConfig`.
- [x] Implement parser/normalizer for `key=value` expressions.
- [x] Support keys: `repository`, `tag`; reject unknown keys.
- [x] Reject malformed expressions (missing `=`, empty key/value).

### Phase 2: GraphQL + matcher integration
- [x] Extend GraphQL query fragments for Issue/PullRequest to include label names.
- [x] Add per-item metadata extraction (`repository`, `tags`) for matching.
- [x] Apply filter matcher before appending mapped tasks.
- [x] Update `limit` accounting to count only matched tasks.

### Phase 3: Docs and compatibility
- [x] Document `provider_config.filters` in `README.md` with examples.
- [x] Update `DESIGN.md` provider config shape/notes.
- [x] Confirm no behavior changes when `filters` is omitted.

### Phase Dependencies

- Phase 2 depends on Phase 1 parser and validated filter model.
- Phase 3 depends on finalized semantics from Phase 2.

---

## Testing

Integration tests:
- [x] Poll with `repository=acme/repo` returns only matching repo tasks.
- [x] Poll with `tag=backend` returns only items with `backend` label.
- [x] Poll with `repository=...` + `tag=...` applies combined AND semantics.
- [x] Poll with multiple `tag=` filters requires all tags.
- [x] Poll with filters + `limit=1` returns first matched task and continues pagination until match found.

Unit tests:
- [x] Filter parsing accepts valid `key=value` entries and normalizes casing/whitespace.
- [x] Parser rejects unsupported keys.
- [x] Parser rejects malformed expressions and empty values.
- [x] Item matcher semantics for repository OR and tag AND are correct.

Manual validation:
- [ ] Configure `provider_config.filters` in `.loops/config.json` for a known project.
- [ ] Run `loops run --run-once` and verify only expected tasks are emitted.
- [ ] Remove filters and confirm previous broad polling behavior is restored.

---

## Dependencies

### External Services or APIs
- GitHub GraphQL API (`projectV2.items`, `Issue.labels`, `PullRequest.labels`) via `gh api graphql`.

### Libraries or Packages
- No new packages required.

### Tools or Infrastructure
- `gh` CLI and GitHub token (`GITHUB_TOKEN`/`GH_TOKEN`) already required by provider.

### Access Required
- [ ] Access to a GitHub Project V2 board with items across multiple repositories and labels.

---

## Risks and Mitigations

| Risk | Impact | Probability | Mitigation |
| --- | --- | --- | --- |
| Filter semantics surprise users (AND/OR expectations) | Med | Med | Document exact semantics with examples in README and errors for invalid keys. |
| Label fetch increases GraphQL payload size | Low | Med | Request only label names and keep existing pagination; monitor test/runtime behavior. |
| Limit behavior regressions under filters | High | Med | Add explicit tests for filtered pagination + limit, including delayed match pages. |
| Ambiguous “tags” meaning across GitHub objects | Med | Low | Define tags explicitly as Issue/PR labels in docs and spec. |

---

## Questions

### Technical Decisions Needed
- [x] Should multiple `tag=` filters be AND or OR?
  - Decision: AND (all configured tags required).
- [x] Should multiple `repository=` filters be AND or OR?
  - Decision: OR (any configured repository).
- [x] Should matching be case-sensitive?
  - Decision: case-insensitive exact-match after normalization.

### Clarifications Required
- [x] Confirm whether we should also accept alias key `repo=` in addition to `repository=`.
  - Decision: no alias in this phase; only explicit `repository=` is supported.

### Research Tasks
- [x] Verify practical label-count edge cases for project items with >100 labels and whether current `labels(first:100)` is sufficient for this scope.
  - Decision: keep `labels(first:100)` as acceptable for MVP and revisit only if real boards exceed this bound.

---

## Success Criteria

- [x] `provider_config.filters` supports `key=value` expressions for repository and tags.
- [x] Provider poll returns only matching tasks when filters are configured.
- [x] `limit` is enforced against matched tasks in filtered mode.
- [x] Existing behavior remains unchanged when filters are absent.
- [x] Tests and docs are updated and passing.

---

## Notes

- Simplification: scope is intentionally limited to repository and label-based tag filtering for GitHub Projects V2 provider only.
- Future extension can add additional keys (for example `type=issue|pr`) without changing `key=value` input style.

## Manual Notes 

[keep this for the user to add notes. do not change between edits]

## Changelog
- 2026-02-18: Created feature spec for GitHub Projects V2 provider filters (`repository`, `tag`) with key=value semantics. (019c6958-6b2d-7dc1-932d-2e6c5e8f79b7)
- 2026-02-18: Implemented provider filter parsing/matching, GraphQL label fetch, tests, and docs updates. (019c6958-6b2d-7dc1-932d-2e6c5e8f79b7)
