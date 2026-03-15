# Execution Plan: Configurable loop handoff handlers with built-in `gh_comment_handler`

**Date:** 2026-02-19
**Status:** Implemented

---

## Goal

Allow selecting the inner-loop handoff strategy from `loop_config`, and add a built-in `gh_comment_handler` that performs handoff via GitHub issue comments derived from the task URL.

Required behavior:
- `loop_config` can select a handoff handler.
- Built-in handlers include existing stdin behavior and new `gh_comment_handler`.
- `gh_comment_handler` depends on `github_projects_v2` tasks and uses the task URL to locate the issue.
- Using `gh_comment_handler` without GitHub Projects V2 task provider should raise an exception.

---

## Context

### Background

Current inner-loop handoff behavior is fixed to stdin (`_default_user_handoff_handler`). This works for interactive runs but is not suitable for unattended/background loops that need an async handoff channel.

### Current State

- `run_inner_loop(..., user_handoff_handler=...)` supports dependency injection for tests, but runtime selection is not configurable from `loop_config`.
- `OuterLoopConfig` has no handoff-handler field.
- Outer loop injects task metadata env vars (`LOOPS_TASK_URL`, `LOOPS_TASK_PROVIDER`) into inner-loop child process.
- `NEEDS_INPUT` currently calls `_handle_needs_input(...)`, which expects a handler response string to continue.

### Temporal Context Check (Required)

| Value | Source of Truth | Representation | Initialization / Capture | First Consumer | Initialized Before Consumption |
| --- | --- | --- | --- | --- | --- |
| `loop_config.handoff_handler` | `.loops/config.json` | string enum | parsed in `load_config` | outer-loop launcher / inner-loop resolver | Yes |
| task provider id | `RunRecord.task.provider_id` and `LOOPS_TASK_PROVIDER` | string | set by outer loop when creating run and launching child | `gh_comment_handler` provider guard | Yes |
| task URL | `RunRecord.task.url` and `LOOPS_TASK_URL` | absolute GitHub issue URL | set at run creation and env injection | issue locator parser in `gh_comment_handler` | Yes |
| handoff payload | `RunRecord.needs_user_input_payload` | dict `{message, context?}` | set by inner-loop signal/state transitions | selected handoff handler | Yes |
| pending prompt/reply state | run-local handler state file | comment ids + payload hash | created/updated by `gh_comment_handler` | next handler invocation in NEEDS_INPUT loop | Yes |

Gate answer:
- Yes. Core values are initialized before handler consumption. The new run-local handler state file is required for idempotency across retries.

### Constraints

- Preserve existing stdin behavior as default.
- Keep runtime dependency footprint unchanged (continue using `gh` CLI).
- Ensure crash/retry-safe behavior in `NEEDS_INPUT` without duplicate issue comment spam.
- Fail fast on unsupported handler/provider combinations.

---

## Technical Approach

### Architecture / Design

Add configurable built-in handoff handlers with a runtime resolver.

Configuration:
- Add `loop_config.handoff_handler` with default `"stdin_handler"`.
- Allowed values (MVP):
  - `stdin_handler`
  - `gh_comment_handler`

Runtime selection:
- Outer loop propagates selected handler to inner loop child (env var or CLI arg; preferred env: `LOOPS_HANDOFF_HANDLER`).
- Inner loop resolves handler by name before processing `NEEDS_INPUT`.

`gh_comment_handler` behavior:
1. Validate task provider is `github_projects_v2`; otherwise raise `ValueError`.
2. Parse `task.url` as GitHub issue URL (`https://github.com/<owner>/<repo>/issues/<number>`); raise if invalid.
3. Post handoff request comment to issue via `gh issue comment` (idempotent: once per unique payload hash).
4. Poll issue comments via `gh issue view --json comments` and look for response comments with explicit prefix (MVP proposal: `/loops-reply ...`) newer than prompt comment.
5. Return parsed response text when found; otherwise indicate waiting.

Handler contract adjustment:
- Update handoff handler contract to support async waiting cleanly (not just immediate string return). Proposed shape:
  - `HandoffResult(status="waiting" | "response", response?: str)`
- `stdin_handler` returns `response` immediately.
- `gh_comment_handler` returns `waiting` until reply comment is found.

Idempotency and state:
- Add run-local file (for example `handoff_gh_comment_state.json`) with:
  - current payload hash
  - prompt comment id/timestamp
  - last consumed reply comment id
- Reuse state on restart to avoid re-posting same prompt.

### Technology Stack

- Existing Python stdlib.
- Existing `gh` CLI subprocess integration style.
- Existing run-local file persistence pattern.

### Integration Points

- `loops/outer_loop.py`
  - `OuterLoopConfig` + config parsing for `handoff_handler`
  - launcher propagation of selected handler
  - validation guard for `gh_comment_handler` + provider compatibility (fail fast)
- `loops/cli.py`
  - `init` default config adds `loop_config.handoff_handler`
  - `doctor` config upgrade path backfills `loop_config.handoff_handler` for older configs
- `loops/inner_loop.py`
  - handoff resolver + updated handoff result handling in `_handle_needs_input`
- New module (recommended): `loops/handoff_handlers.py`
  - built-in handlers + GitHub issue comment integration + URL parsing + state tracking
- Docs:
  - `README.md`, `DESIGN.md`, and flow docs if behavior contract changes materially

### Design Patterns

- Strategy pattern for built-in handoff handlers.
- Explicit compatibility guards (`provider_id` check).
- Retry-safe idempotency via run-local state file keyed by payload hash.

### Important Context

- This feature must not change state-machine semantics (`NEEDS_INPUT` remains authoritative).
- `gh_comment_handler` is tightly coupled to GitHub issue URL format and provider identity.
- Prompt/reply protocol should be explicit to avoid consuming unrelated issue comments.

---

## Steps

### Phase 1: Config + wiring
- [x] Add `handoff_handler` to `OuterLoopConfig` (default `stdin_handler`).
- [x] Load and validate value from `loop_config`.
- [x] Ensure `upgrade_config_payload`/`loops doctor` backfill `loop_config.handoff_handler` for legacy configs.
- [x] Propagate selected handler from outer loop to inner-loop runtime context.
- [x] Add fail-fast validation: if handler is `gh_comment_handler` and provider is not `github_projects_v2`, raise exception.

### Phase 2: Built-in handler framework
- [x] Introduce built-in handler resolver (`stdin_handler`, `gh_comment_handler`).
- [x] Refactor `_handle_needs_input` to support waiting vs response outcomes.
- [x] Keep existing stdin handler behavior unchanged under `stdin_handler`.

### Phase 3: `gh_comment_handler` implementation
- [x] Parse/validate GitHub issue URL from task URL.
- [x] Implement `gh issue comment` posting for handoff request.
- [x] Implement comment polling + `/loops-reply` response extraction.
- [x] Add idempotent run-local state tracking to prevent duplicate prompt comments.

### Phase 4: Docs + hardening
- [x] Document new `loop_config.handoff_handler` options.
- [x] Document response comment protocol for `gh_comment_handler`.
- [x] Add clear runtime error messages for unsupported provider/task URL combinations.

### Phase Dependencies

- Phase 2 depends on Phase 1 wiring.
- Phase 3 depends on Phase 2 contract.
- Phase 4 depends on finalized behavior and error messages.

---

## Testing

Integration tests:
- [x] Loop with `handoff_handler=stdin_handler` preserves existing interactive behavior.
- [x] Loop with `handoff_handler=gh_comment_handler` posts handoff prompt to issue and stays waiting until reply.
- [x] Reply comment with `/loops-reply ...` is consumed and inner loop resumes.
- [x] `gh_comment_handler` configured with non-GitHub provider raises exception early.

Unit tests:
- [x] `loop_config.handoff_handler` parsing/validation.
- [x] Handler resolver maps names to correct built-ins.
- [x] Config upgrade (`upgrade_config_payload`) adds default `handoff_handler` when missing.
- [x] GitHub issue URL parser accepts valid issue URLs and rejects non-issue URLs.
- [x] Idempotency state prevents duplicate prompt comments across retries.
- [x] Reply parser extracts response only from valid prefixed comments newer than prompt.

Manual validation:
- [ ] Configure `.loops/config.json` with `handoff_handler=gh_comment_handler`.
- [ ] Trigger `NEEDS_INPUT` and verify comment appears on task issue.
- [ ] Post `/loops-reply <text>` on issue and verify loop resumes.
- [ ] Misconfigure with non-GitHub provider and confirm explicit exception.

---

## Dependencies

### External Services or APIs
- GitHub issue comments via `gh issue comment` / `gh issue view --json comments`.

### Libraries or Packages
- No new packages required.

### Tools or Infrastructure
- `gh` CLI with auth token (already required for GitHub provider usage).

### Access Required
- [ ] Write access to GitHub issue comments for target repositories.

---

## Risks and Mitigations

| Risk | Impact | Probability | Mitigation |
| --- | --- | --- | --- |
| Duplicate handoff comments during retries | High | Med | Persist payload-hash + prompt-comment state and post once per payload revision. |
| Consuming wrong issue comment as response | High | Med | Require explicit prefix (`/loops-reply`) and strict ordering after prompt comment. |
| Provider mismatch not caught until late runtime | Med | Med | Validate handler/provider compatibility in outer-loop config path and in handler resolver. |
| Task URL not an issue URL (e.g. PR URL) | Med | Med | Strict URL parsing with clear exception and remediation guidance. |

---

## Questions

### Technical Decisions Needed
- [x] Handler naming in config.
  - Decision: `stdin_handler` (default) and `gh_comment_handler`.
- [x] Reply protocol for GitHub comments.
  - Decision: explicit `/loops-reply` prefix to avoid accidental parsing.
- [x] Validation location for provider compatibility.
  - Decision: validate both in outer-loop configuration path (early) and inner-loop handler resolution (defensive).

### Clarifications Required
- [ ] Whether to allow PR URLs (`/pull/<n>`) for comment handoff in addition to issue URLs.

### Research Tasks
- [x] Verify `gh issue view --json comments` payload fields used for robust comment id/timestamp tracking across API versions.

---

## Success Criteria

- [x] `loop_config` can choose built-in handoff handler.
- [x] `stdin_handler` remains behavior-compatible with current flow.
- [x] `gh_comment_handler` posts handoff prompts to GitHub issue and resumes on explicit reply comments.
- [x] Using `gh_comment_handler` without GitHub Projects V2 task provider throws explicit exception.
- [x] Tests and docs are updated and passing.

---

## Notes

- This plan scopes handler selection to built-in names only (no arbitrary command/plugin system yet).
- `gh_comment_handler` is provider-coupled by design for MVP safety and clearer failure semantics.

## Manual Notes 

[keep this for the user to add notes. do not change between edits]

## Changelog
- 2026-02-19: Created feature spec for configurable handoff handler selection and built-in `gh_comment_handler`. (019c747a-a05e-7be1-b09d-66c5debb37c4)
- 2026-02-19: Implemented configurable handoff handlers, gh issue comment handoff flow, config wiring/doctor defaults, tests, and docs updates. (019c747a-a05e-7be1-b09d-66c5debb37c4)
