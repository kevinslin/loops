# Feature Spec: Harness Engineering for Loops

**Date:** 2026-03-01
**Status:** Planning

---

## Goal and Scope

### Goal
Apply harness-engineering best practices to Loops so agents can execute reliably with less human babysitting, by making repository intent machine-legible, enforcing invariants mechanically, tightening feedback loops, and continuously controlling entropy.

### In Scope
- Codify Loops repository knowledge as an explicit system of record for agents.
- Add mechanical checks for documentation contracts and runtime invariants.
- Strengthen inner-loop and outer-loop feedback loops with harness-focused integration tests.
- Add agent-legible observability artifacts alongside existing human logs.
- Add repeatable cleanup and garbage-collection workflows for `.loops/` runtime state.

### Out of Scope
- Replacing the GitHub Projects V2 provider model.
- Building a web UI for harness observability.
- Introducing non-Codex execution engines.
- Changing core run-state semantics (`RUNNING`, `WAITING_ON_REVIEW`, `NEEDS_INPUT`, `PR_APPROVED`, `DONE`).

---

## Context and Constraints

### Background
OpenAI's harness-engineering guidance emphasizes operating the codebase as an agent runtime, not just a prompt. The key principles relevant to Loops are:
- Treat durable instructions and architecture as a system of record.
- Optimize for agent legibility, not only human readability.
- Enforce architecture and coding expectations with mechanical checks.
- Assume higher throughput changes merge/review dynamics, so harness controls must shift left.
- Manage entropy continuously with intentional cleanup.

Reference: https://openai.com/index/harness-engineering/

### Current State
- Core architecture and contracts exist in [DESIGN.md](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/DESIGN.md), [ref.inner-loop.md](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/docs/flows/ref.inner-loop.md), and [ref.outer-loop.md](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/docs/flows/ref.outer-loop.md).
- Runtime behavior is implemented in [outer_loop.py](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/loops/outer_loop.py), [inner_loop.py](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/loops/inner_loop.py), [run_record.py](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/loops/run_record.py), and [github_projects_v2.py](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/loops/providers/github_projects_v2.py).
- Test coverage is strong at unit level in [tests/test_inner_loop.py](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/tests/test_inner_loop.py), [tests/test_outer_loop.py](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/tests/test_outer_loop.py), [tests/test_cli.py](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/tests/test_cli.py), and provider/run-record tests.
- Gaps for harness engineering:
  - No repo-local automated checker that verifies docs and runtime invariants stay aligned.
  - Logs are human-readable text (`oloops.log`, `run.log`, `agent.log`) but not strongly machine-consumable for agent debugging/replay.
  - No first-class janitor/garbage-collection workflow for stale `.loops/jobs/*` runs and signal artifacts.
  - No dedicated end-to-end harness test suite that validates outer-loop to inner-loop handoff plus artifact contracts as one flow.

### Required Pre-Read (LLM Agent)
- [AGENTS.md](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/AGENTS.md)
- [DESIGN.md](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/DESIGN.md)
- [ref.outer-loop.md](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/docs/flows/ref.outer-loop.md)
- [ref.inner-loop.md](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/docs/flows/ref.inner-loop.md)
- [cli.py](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/loops/cli.py)
- [outer_loop.py](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/loops/outer_loop.py)
- [inner_loop.py](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/loops/inner_loop.py)
- [run_record.py](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/loops/run_record.py)
- [github_projects_v2.py](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/loops/providers/github_projects_v2.py)
- [tests/test_outer_loop.py](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/tests/test_outer_loop.py)
- [tests/test_inner_loop.py](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/tests/test_inner_loop.py)
- [tests/test_cli.py](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/tests/test_cli.py)
- https://openai.com/index/harness-engineering/

### Temporal Context Check (Required)

| Value | Source of Truth | Representation | Initialization / Capture | First Consumer | Initialized Before Consumption |
| --- | --- | --- | --- | --- | --- |
| Loop config defaults | `build_default_loop_config_payload` in `loops/outer_loop.py` | JSON object in config payload | `loops init`, `loops doctor`, `load_config` | outer-loop runner and launcher wiring | Yes |
| Run state derivation | `derive_run_state` in `loops/run_record.py` | enum string | on every `write_run_record` and loop iteration | inner-loop state dispatch in `run_inner_loop` | Yes |
| Outer dedupe ledger | `.loops/outer_state.json` via `read_outer_state`/`write_outer_state` | JSON snapshot | start/end of `run_once` | task selection and emit gate | Yes |
| NEEDS_INPUT payload | `needs_user_input_payload` in `run.json` | JSON object | `_run_codex_turn` / `_force_needs_input` | handoff handlers + next Codex prompt | Yes |
| Documentation contract | `DESIGN.md` + flow docs + AGENTS.md | markdown sections + invariants | currently manual updates | human and LLM agents | No (currently not mechanically validated) |
| Runtime artifact contract | `.loops/jobs/<run>/run.json`, logs, signal files | files + JSON | outer-loop run materialization and inner-loop writes | operators and debugging agents | Partially (schema exists, but no global artifact validator) |

Gate answer:
- `No`/`Partially` values are concentrated in documentation and artifact validation. This spec prioritizes those before adding broader automation.

### Constraints
- Preserve current command UX (`loops`, `python -m loops`) and existing config compatibility behavior.
- Keep existing single-writer ownership of `run.json` (inner loop only).
- Avoid requiring network access for default harness checks; live GitHub checks must be opt-in.
- Keep new harness checks deterministic and CI-friendly.
- Any future `loop_config` schema/default changes must update `loops doctor` upgrade behavior and tests.

### Non-obvious Dependencies or Access (Optional)
- `gh` CLI and GitHub token are required only for live provider workflows, not for default harness-check execution.
- CI needs permission to run additional scripts under `scripts/` and integration tests under `tests/`.

---

## Approach and Touchpoints

### Proposed Approach
Implement harness engineering as five practical workstreams aligned to OpenAI guidance:
1. System-of-record codification.
2. Mechanical checks for docs and code invariants.
3. Stronger inner/outer loop feedback tests.
4. Agent-legible observability artifacts.
5. Continuous cleanup and entropy control.

### Integration Points / Touchpoints
- Existing architecture and docs:
  - [DESIGN.md](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/DESIGN.md)
  - [ref.outer-loop.md](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/docs/flows/ref.outer-loop.md)
  - [ref.inner-loop.md](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/docs/flows/ref.inner-loop.md)
- Core runtime modules:
  - [loops/cli.py](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/loops/cli.py)
  - [loops/outer_loop.py](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/loops/outer_loop.py)
  - [loops/inner_loop.py](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/loops/inner_loop.py)
  - [loops/run_record.py](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/loops/run_record.py)
  - [loops/logging_utils.py](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/loops/logging_utils.py)
  - [loops/providers/github_projects_v2.py](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/loops/providers/github_projects_v2.py)
- Existing test suites to extend:
  - [tests/test_cli.py](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/tests/test_cli.py)
  - [tests/test_outer_loop.py](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/tests/test_outer_loop.py)
  - [tests/test_inner_loop.py](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/tests/test_inner_loop.py)
  - [tests/test_run_record.py](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/tests/test_run_record.py)
- Planned new harness artifacts:
  - `scripts/harness/check_system_of_record.py`
  - `scripts/harness/check_runtime_invariants.py`
  - `scripts/harness/check_run_artifacts.py`
  - `scripts/harness/janitor.py`
  - `tests/test_harness_checks.py`
  - `tests/test_harness_observability.py`
  - `tests/test_harness_janitor.py`
  - `tests/integ/test_harness_end_to_end.py`

### Resolved Ambiguities / Decisions
- System-of-record policy: `AGENTS.md`, `DESIGN.md`, and the two reference flow docs are the canonical operational contract for Loops behavior.
- Enforcement strategy: codify expectations in executable checks, not prose-only conventions.
- Observability strategy: keep existing text logs, add structured JSONL event artifacts in parallel.
- Feedback strategy: keep unit tests and add harness-level integration tests for outer-to-inner flow and artifact generation.
- Entropy strategy: add explicit janitor tooling with `--dry-run` default and conservative deletion guards.

### Important Implementation Notes (Optional)
- New harness checks should avoid brittle string matching when possible; prefer structural checks (required keys, known states, parseable JSON, invariant predicates).
- All new scripts should return non-zero exit codes with actionable error messages for CI and agent loops.

---

## Phases and Dependencies

### Phase 1: Codify System of Record
- [ ] Define and document canonical harness contract sections in:
  - [AGENTS.md](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/AGENTS.md)
  - [DESIGN.md](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/DESIGN.md)
  - [docs/flows/ref.outer-loop.md](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/docs/flows/ref.outer-loop.md)
  - [docs/flows/ref.inner-loop.md](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/docs/flows/ref.inner-loop.md)
- [ ] Add a stable Required Pre-Read block for agents in the canonical docs and enforce ordering/format.
- [ ] Record key invariants (state precedence, dedupe behavior, signal ownership, approval override rules) as machine-checkable expectations.

### Phase 2: Add Mechanical Checks and Linters
- [ ] Implement `scripts/harness/check_system_of_record.py`:
  - validate required docs exist and contain required sections.
  - validate Required Pre-Read entries resolve to real paths/URLs.
- [ ] Implement `scripts/harness/check_runtime_invariants.py`:
  - verify `derive_run_state` precedence invariants.
  - verify outer-loop defaults alignment between init/doctor/runtime loaders.
  - verify required run artifacts are created during scheduling (`run.json`, `run.log`, `agent.log`, `inner_loop_runtime_config.json`).
- [ ] Wire harness checks into test/CI pathways via:
  - [Makefile](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/Makefile)
  - [tests/test_cli.py](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/tests/test_cli.py)
  - new `tests/test_harness_checks.py`.

### Phase 3: Strengthen Inner/Outer Feedback Loops
- [ ] Add harness integration suite `tests/integ/test_harness_end_to_end.py` with deterministic Codex and provider stubs.
- [ ] Cover end-to-end scenarios not currently asserted as one flow:
  - outer-loop selection and run materialization.
  - handoff to inner loop with expected env contract.
  - NEEDS_INPUT handoff behavior plus resume behavior.
  - review feedback loops including comment-based feedback and approval paths.
- [ ] Reuse and extend existing behaviors already unit-tested in:
  - [tests/test_outer_loop.py](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/tests/test_outer_loop.py)
  - [tests/test_inner_loop.py](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/tests/test_inner_loop.py)
  - [tests/test_github_projects_v2_provider.py](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/tests/test_github_projects_v2_provider.py).

### Phase 4: Add Agent-Legible Observability Artifacts
- [ ] Add structured event-writing utilities (JSONL) for outer-loop and inner-loop milestones in:
  - [loops/logging_utils.py](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/loops/logging_utils.py)
  - [loops/outer_loop.py](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/loops/outer_loop.py)
  - [loops/inner_loop.py](/Users/kevinlin/.worktrees/loops/dev/2026-02-09-create-integration-testing-harness-for-loops/loops/inner_loop.py).
- [ ] Emit parseable per-run artifact files (for example `events.jsonl` and `summary.json`) without removing existing text logs.
- [ ] Add artifact schema tests in `tests/test_harness_observability.py` and regression assertions in existing log-related tests.

### Phase 5: Continuous Cleanup and Garbage Collection
- [ ] Implement `scripts/harness/janitor.py` for stale run cleanup with:
  - `--loops-root`
  - `--older-than-days`
  - `--dry-run` (default)
  - explicit apply flag for destructive mode.
- [ ] Ensure janitor never removes active runs (for example based on recent file mtime and/or explicit lock markers).
- [ ] Add tests in `tests/test_harness_janitor.py` for dry-run reporting, safe deletion boundaries, and idempotency.
- [ ] Document local/CI cadence for janitor runs and retention guidance.

### Phase Dependencies
- Phase 2 depends on Phase 1 system-of-record contract.
- Phase 3 depends on Phase 2 harness checks baseline.
- Phase 4 depends on Phase 3 end-to-end coverage to validate event semantics.
- Phase 5 depends on Phase 4 artifact contract so cleanup can make safe decisions.

---

## Validation and Done Criteria

### Validation Plan

Integration tests:
- `python -m pytest tests/integ/test_harness_end_to_end.py`
- `python -m pytest tests/test_outer_loop.py tests/test_inner_loop.py`
- `python -m pytest tests/test_cli.py tests/test_run_record.py`

Unit tests (Optional):
- `python -m pytest tests/test_harness_checks.py`
- `python -m pytest tests/test_harness_observability.py`
- `python -m pytest tests/test_harness_janitor.py`

Mechanical checks:
- `python scripts/harness/check_system_of_record.py`
- `python scripts/harness/check_runtime_invariants.py`
- `python scripts/harness/check_run_artifacts.py --loops-root .loops`

Manual validation:
- Run one `loops run --run-once` scenario with a deterministic inner-loop stub and confirm run artifacts include parseable structured events.
- Trigger `NEEDS_INPUT` via run-state mutation (for example failing Codex turn or fixture with `needs_user_input=true`) and confirm transition appears in both `run.json` and structured artifact output.
- Run janitor in dry-run and apply modes on a fixture `.loops/` tree and verify only stale runs are removed.

### Done Criteria
- [ ] System-of-record docs contain required harness sections and pass `check_system_of_record.py` with exit code `0`.
- [ ] Runtime invariant checker fails on injected invariant violations and passes on clean mainline.
- [ ] Harness integration suite is present and green in local CI command set.
- [ ] Each new run directory emits parseable agent-legible artifact files in addition to text logs.
- [ ] Janitor tooling ships with dry-run default, tests, and documented safe usage.
- [ ] Existing `python -m pytest` remains green after harness additions.

---

## Open Items and Risks

### Open Items
- [ ] Decide whether structured event schema versioning should live in `loops/run_record.py` or separate artifact metadata.
- [ ] Decide default retention policy (`older-than-days`) for janitor in local dev vs CI environments.
- [ ] Decide whether live GitHub provider integration tests run only opt-in or in scheduled CI with scoped credentials.

### Risks and Mitigations

| Risk | Impact | Probability | Mitigation |
| --- | --- | --- | --- |
| Checkers become brittle and noisy | Med | Med | Favor structural predicates over literal string matching; include golden fixtures and clear error messages. |
| Integration harness becomes flaky | High | Med | Keep default integration tests deterministic with local stubs; gate live network tests behind explicit env flags. |
| Structured artifact growth increases disk usage | Med | High | Add retention controls and periodic janitor pass; keep artifact schema compact and bounded. |
| Cleanup deletes useful data | High | Low | Dry-run by default, require explicit apply flag, and skip recent/active runs. |
| Drift between docs and runtime still slips through | High | Med | Enforce checks in CI and local pre-merge workflow; fail fast on required section/invariant mismatches. |

### Simplifications and Assumptions (Optional)
- This plan assumes harness checks are repo-local scripts plus pytest coverage, not a separate service.
- This plan assumes no immediate `loop_config` schema expansion is required to deliver baseline cleanup and observability improvements.

## Manual Notes 

[keep this for the user to add notes. do not change between edits]

## Changelog
- 2026-03-01: Created feature spec for harness-engineering investments in Loops, aligned with OpenAI guidance and repo touchpoints. (019ca771-d6ce-7712-b315-a12f5d46eb4b)
