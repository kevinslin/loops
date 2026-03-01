# Research Brief: GitHub Actions CI Status Values and Loops Normalization

**Last Updated**: 2026-03-01

**Status**: Complete

**Related**:

- `DESIGN.md`
- `docs/flows/ref.inner-loop.md`
- `loops/inner_loop.py`
- `loops/run_record.py`

* * *

## Executive Summary

This brief documents the different CI status vocabularies exposed by GitHub (GraphQL enums, Checks APIs, Workflow Runs APIs, and legacy Commit Status APIs) and maps them to Loops' normalized `ci_status` model (`pending | success | failure`).

The key finding is that GitHub uses overlapping but non-identical status/conclusion sets across API surfaces, and Loops intentionally collapses that larger set into three values. That collapse is mostly correct for merge-gating, but it hides distinctions (for example `neutral`, `skipped`, `action_required`, and `expected`) that can matter for diagnostics or policy decisions.

**Research Questions**:

1. What are the authoritative CI status/conclusion values across GitHub API surfaces relevant to pull-request CI?

2. Which values are GitHub Actions-only versus general checks/status values?

3. How does Loops currently normalize these values, and where are the semantic gaps?

* * *

## Research Methodology

### Approach

- Queried GitHub's live GraphQL schema via introspection (`gh api graphql`) for enum values.
- Reviewed GitHub official docs for REST Check Runs, Workflow Runs, Commit Statuses, and Status Checks behavior.
- Traced Loops inner-loop CI parsing from PR polling (`gh pr view`) through normalization and state transitions.

### Sources

- GitHub GraphQL enum reference (`CheckStatusState`, `CheckConclusionState`, `CheckRunState`, `StatusState`): https://docs.github.com/en/graphql/reference/enums
- GitHub REST checks (check runs): https://docs.github.com/en/rest/checks/runs?apiVersion=2022-11-28
- GitHub REST actions workflow runs: https://docs.github.com/en/rest/actions/workflow-runs?apiVersion=2022-11-28
- GitHub REST commit statuses: https://docs.github.com/en/rest/commits/statuses?apiVersion=2022-11-28
- GitHub status checks behavior doc: https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/collaborating-on-repositories-with-code-quality-features/about-status-checks
- Local code paths: `loops/inner_loop.py`, `loops/run_record.py`, `tests/test_inner_loop.py`

* * *

## Research Findings

### Context Triage Gate (Temporal Ordering)

| Value | Source of truth | Representation | Initialization point | Snapshot point | First consumer | Initialized before capture? |
| --- | --- | --- | --- | --- | --- | --- |
| PR status payload | `gh pr view --json ... statusCheckRollup` | JSON object | `loops/inner_loop.py:1744` subprocess call | parsed JSON at `loops/inner_loop.py:1770` | `_ci_status_from_rollup` at `loops/inner_loop.py:1801` | Yes |
| `statusCheckRollup` entries | GitHub PR payload | list of status contexts/check runs | same payload | read at `loops/inner_loop.py:1469` | per-item parser in `_ci_status_from_rollup` | Yes |
| Normalized `RunPR.ci_status` | Loops `RunPR` | `pending | success | failure` | `_ci_status_from_rollup` at `loops/inner_loop.py:1468` | persisted to `run.json` at `loops/inner_loop.py:1879` | auto-approve/state derivation | Yes |
| Auto-approve CI gate | Loops runtime | boolean branch (`ci_status == success`) | read at `loops/inner_loop.py:546` | N/A | `_run_auto_approve_eval` gate | Yes |
| Run-state transition gate | `derive_run_state` | conditional on `pr.ci_status` | read at `loops/run_record.py:245` | N/A | state machine routing | Yes |

Ordering is valid in current flow: normalized `ci_status` is always derived before CI-dependent decisions.

* * *

### GitHub CI Status Taxonomy by API Surface

#### GraphQL (authoritative enums)

**Status**: Complete

**Details**:

- Live introspection (`gh api graphql`) currently returns:
  - `CheckStatusState`: `REQUESTED`, `QUEUED`, `IN_PROGRESS`, `COMPLETED`, `WAITING`, `PENDING`
  - `CheckConclusionState`: `ACTION_REQUIRED`, `TIMED_OUT`, `CANCELLED`, `FAILURE`, `SUCCESS`, `NEUTRAL`, `SKIPPED`, `STARTUP_FAILURE`, `STALE`
  - `StatusState`: `EXPECTED`, `ERROR`, `FAILURE`, `PENDING`, `SUCCESS`
  - `StatusCheckRollupContext` union: `CheckRun` or `StatusContext`
- This confirms that PR rollups can mix check-run data and commit-status-style contexts.

**Assessment**: GraphQL schema is the cleanest canonical reference for enum completeness.

* * *

#### REST Checks API (check run lifecycle and conclusions)

**Status**: Complete

**Details**:

- Check run `status` includes `queued`, `in_progress`, `completed`, `waiting`, `requested`, `pending`.
- Docs explicitly note `requested`, `waiting`, and `pending` are GitHub Actions-only statuses.
- When completed, check run `conclusion` includes `action_required`, `cancelled`, `failure`, `neutral`, `success`, `skipped`, `stale`, `timed_out`.

**Assessment**: REST checks docs match GraphQL status/conclusion families and clarify Actions-only states.

* * *

#### REST Workflow Runs API (mixed filter vocabulary)

**Status**: Complete

**Details**:

- Workflow-runs `status` filter accepts both run statuses and conclusion-like values in one parameter.
- Allowed values include `completed`, `in_progress`, `queued`, `requested`, `waiting`, `pending`, plus outcome values like `success`, `failure`, `cancelled`, `neutral`, `skipped`, `stale`, `timed_out`, `action_required`.
- Docs also mark `waiting`, `pending`, and `requested` as GitHub Actions-only.

**Assessment**: The mixed filter parameter is easy to misread; treat it as search vocabulary, not a single enum for run-state modeling.

* * *

#### REST Commit Statuses API (legacy external CI model)

**Status**: Complete

**Details**:

- Commit statuses expose `error`, `failure`, `pending`, `success`.
- Combined status rolls up to `failure`, `pending`, or `success`.
- `StatusContext` values in GraphQL rollups align to this older model (`EXPECTED`, `ERROR`, `FAILURE`, `PENDING`, `SUCCESS`).

**Assessment**: Still relevant because PR rollups can include status contexts from non-check integrations.

* * *

### Loops Normalization Behavior

#### Current normalization logic

**Status**: Complete

**Details**:

- Loops stores only three CI values: `pending | success | failure` (`loops/run_record.py:11`).
- `_ci_status_from_rollup` behavior (`loops/inner_loop.py:1468`):
  - Missing/empty rollup => `pending`.
  - Pending-like states (`EXPECTED`, `PENDING`, `QUEUED`, `IN_PROGRESS`, `WAITING`, `REQUESTED`) => `pending`.
  - Success-like conclusions (`SUCCESS`, `NEUTRAL`, `SKIPPED`) => `success`.
  - Failure-like conclusions (`FAILURE`, `CANCELLED`, `TIMED_OUT`, `ACTION_REQUIRED`, `STARTUP_FAILURE`, `STALE`) and `ERROR` => `failure`.
  - Unknown non-success conclusions default to `failure`.
- Auto-approve gate only opens when normalized status is `success` (`loops/inner_loop.py:546`, `loops/run_record.py:245`).

**Assessment**: Deliberately conservative; good for safety gates, lossy for debugging and policy nuance.

* * *

#### Key semantic gaps introduced by normalization

**Status**: Complete

**Details**:

- `neutral` and `skipped` are treated as `success` (consistent with GitHub's dependent-check behavior), but policy may differ for some teams.
- `action_required` is collapsed into `failure`; this is safe for gating, but not actionably descriptive.
- `expected` (status context waiting for report) is collapsed into `pending`; this is usually correct but can mask integration drift.
- Unknown future statuses default to `pending` (if interpreted as active) or `failure` (if interpreted as non-success conclusion), which can produce behavior shifts after upstream API changes.

**Assessment**: Current mapping favors safety and progress gating over observability precision.

* * *

## Comparative Analysis

| Criteria | Raw GitHub values (no collapse) | Current Loops normalized model (`pending/success/failure`) | Expanded normalized model (example: + `neutral`, `blocked`, `cancelled`) |
| --- | --- | --- | --- |
| Fidelity to GitHub semantics | High | Low-Medium | Medium-High |
| Simplicity for state machine gates | Low | High | Medium |
| Backward compatibility with current Loops logic | N/A | High | Low-Medium |
| Debuggability | High | Medium | High |
| Risk of accidental merge on ambiguous CI | Medium | Low | Low-Medium |

**Strengths/Weaknesses Summary**:

- **Raw values**: Maximum detail, but complicates run-state decisions.
- **Current Loops model**: Minimal and robust for gating; hides actionable distinctions.
- **Expanded model**: Better diagnostics/policy controls at the cost of migration and additional logic.

* * *

## Best Practices

1. **Document API surface explicitly**: Always state whether a value comes from check `status`, check `conclusion`, workflow filter vocabulary, or commit status context.

2. **Treat Actions-only states specially**: `requested`, `waiting`, and `pending` can indicate orchestration conditions rather than failing CI.

3. **Keep normalization conservative for merge gates**: Mapping unknown/non-success conclusions to failure avoids accidental promotion.

4. **Persist richer raw diagnostics when possible**: Store raw rollup values in logs even if run-state uses a collapsed enum.

5. **Validate enum drift periodically**: Use GraphQL introspection in CI/docs checks to detect upstream value additions.

* * *

## Open Research Questions

1. **Should `neutral` and `skipped` remain auto-approve eligible?** This is currently treated as success; some teams may require strict `success` only.

2. **Do we want a "blocked" class for `action_required` / `waiting`?** This could improve handoff messaging without merging risk.

3. **Should raw rollup snapshots be persisted in `run.json`?** It would improve postmortem quality and reduce ambiguity.

* * *

## Recommendations

### Summary

Keep the current 3-value gate for safety and simplicity, but augment observability by recording raw GitHub CI states/conclusions alongside normalized status.

### Recommended Approach

- Preserve current `RunPR.ci_status` enum (`pending | success | failure`) for state transitions.
- Add lightweight raw-status diagnostics (for example in logs and/or optional run metadata):
  - observed `status` values
  - observed `conclusion` values
  - observed `StatusContext.state` values
- Add explicit docs note that `neutral`/`skipped` are intentionally treated as success.
- Add a periodic schema-check step using GraphQL introspection to detect enum drift.

**Rationale**:

- Avoids churn in core state machine behavior.
- Improves operator visibility when CI appears "green" but semantics are nuanced.
- Makes future policy changes data-driven instead of speculative.

### Alternative Approaches

- Expand `RunPR.ci_status` to more states now:
  - Better precision, but requires schema migration and transition rewrites.
- Preserve only current collapsed model with no extra diagnostics:
  - Lowest implementation effort, but recurring ambiguity for failures and blocked states.

* * *

## References

- GitHub GraphQL Enums: https://docs.github.com/en/graphql/reference/enums
- GitHub Checks API (check runs): https://docs.github.com/en/rest/checks/runs?apiVersion=2022-11-28
- GitHub Workflow Runs API: https://docs.github.com/en/rest/actions/workflow-runs?apiVersion=2022-11-28
- GitHub Commit Statuses API: https://docs.github.com/en/rest/commits/statuses?apiVersion=2022-11-28
- About status checks: https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/collaborating-on-repositories-with-code-quality-features/about-status-checks
- Loops CI parsing and state gates: `loops/inner_loop.py`, `loops/run_record.py`, `tests/test_inner_loop.py`

* * *

## Appendices

### Appendix A: Live GraphQL Introspection Snapshot (2026-03-01)

```bash
gh api graphql -f query='query { __type(name: "CheckStatusState") { enumValues { name } } }'
# REQUESTED, QUEUED, IN_PROGRESS, COMPLETED, WAITING, PENDING

gh api graphql -f query='query { __type(name: "CheckConclusionState") { enumValues { name } } }'
# ACTION_REQUIRED, TIMED_OUT, CANCELLED, FAILURE, SUCCESS, NEUTRAL, SKIPPED, STARTUP_FAILURE, STALE

gh api graphql -f query='query { __type(name: "StatusState") { enumValues { name } } }'
# EXPECTED, ERROR, FAILURE, PENDING, SUCCESS
```

### Appendix B: Loops Mapping Table

| Raw value family | Raw examples | Loops normalized |
| --- | --- | --- |
| Pending-like | `EXPECTED`, `PENDING`, `QUEUED`, `IN_PROGRESS`, `WAITING`, `REQUESTED` | `pending` |
| Success-like | `SUCCESS`, `NEUTRAL`, `SKIPPED` | `success` |
| Failure-like | `ERROR`, `FAILURE`, `CANCELLED`, `TIMED_OUT`, `ACTION_REQUIRED`, `STARTUP_FAILURE`, `STALE` | `failure` |

## Manual Notes 

[keep this for the user to add notes. do not change between edits]

## Changelog
- 2026-03-01: Added research brief on GitHub Actions/Checks CI status values and Loops normalization behavior. (019ca6d6-fe69-71b2-afff-e68357a6a8d0)
