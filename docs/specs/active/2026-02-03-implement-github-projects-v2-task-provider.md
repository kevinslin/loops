# Execution Plan: Implement GitHub Projects V2 Task Provider

**Date:** 2026-02-03
**Status:** Completed

---

## Goal

Implement a GitHub Projects V2 TaskProvider that polls a configured project URL + status field and returns `Task` objects with all required fields.

---

## Context

### Background
The Loops MVP needs a single task provider that can fetch ready work from GitHub Projects V2. The design doc defines the TaskProvider interface and required `Task` fields, but no provider exists yet.

### Current State
The repo currently has the `loops` package with `RunRecord`/`Task` dataclasses and tests. There is no provider implementation or GraphQL integration.

### Constraints
- Python-only implementation (matches existing codebase).
- Use GitHub Projects V2 APIs (GraphQL). Prefer `gh` CLI for auth.
- Must map project items into `Task` with `provider_id`, `id`, `title`, `status`, `url`, `created_at`, `updated_at`, `repo`.

---

## Technical Approach

### Architecture/Design
- Add a provider module that encapsulates parsing the project URL, querying GitHub GraphQL via `gh api graphql`, and mapping results into `Task`.
- Expose a small `TaskProvider` protocol and a `GithubProjectsV2TaskProvider` class.

### Technology Stack
- Python 3.12 stdlib (`subprocess`, `json`, `dataclasses`, `typing`, `urllib.parse`).
- GitHub CLI (`gh`) for GraphQL API calls.

### Integration Points
- `loops.run_record.Task` for task mapping.
- `gh api graphql` for project item retrieval.

### Design Patterns
- Thin provider class with helper functions for parsing and mapping.
- Defensive parsing for missing status field values or unsupported item types.

### Important Context
- Project URLs are expected to use `/orgs/{org}/projects/{number}` or `/users/{user}/projects/{number}` (extra trailing segments ignored).
- Project item status is read via `fieldValueByName(name: $status_field)` and mapped to a string.

---

## Steps

### Phase 1: Provider implementation
- [x] Add a `TaskProvider` protocol (poll method) and `GithubProjectsV2TaskProviderConfig`.
- [x] Implement `GithubProjectsV2TaskProvider` with:
  - URL parsing for org/user projects.
  - GraphQL query via `gh api graphql` with pagination and limit handling.
  - Mapping project items into `Task` objects.

### Phase 2: Tests
- [x] Add unit tests for URL parsing, status parsing, and task mapping.
- [x] Add a provider poll test with a stubbed `subprocess.run` result.

**Dependencies between phases:**
- Phase 2 depends on Phase 1 implementation details.

---

## Testing

- `python -m pytest`

---

## Dependencies

### External Services/APIs
- GitHub Projects V2 GraphQL API (via `gh api graphql`).

### Tools/Infrastructure
- `gh` CLI must be authenticated to access the project.

---

## Risks & Mitigations

| Risk | Impact | Probability | Mitigation Strategy |
|------|--------|-------------|---------------------|
| GraphQL query shape mismatches actual schema | Med | Low | Keep query minimal, add unit tests around parsing, fail fast on API errors. |
| Project items without issue/PR content | Low | Med | Skip unsupported items and continue polling. |
| Missing status values | Low | Med | Default to empty string and allow outer loop filtering. |

---

## Questions

### Technical Decisions Needed
- [x] Should the provider include draft items (no issue/PR content), or skip them? (Skip; only Issue/PR content mapped.)

---

## Success Criteria

- [x] Provider can poll a project URL and return `Task` objects with required fields (validated via mocked GraphQL responses; live validation pending).
- [x] Empty results return an empty list without errors.
- [x] Tests cover parsing and mapping logic.

---

## Notes

- Reviewed recent commits (last 5) and kept the plan focused on a single provider module + tests with no extra abstractions.
