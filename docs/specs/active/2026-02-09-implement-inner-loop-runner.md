# Execution Plan: Implement inner loop runner

**Date:** 2026-02-09
**Status:** Completed

---

## Goal

Implement the inner loop runner that executes Codex with the unified prompt, appends output to `run.log`, and updates `run.json` with `codex_session.id` plus `needs_user_input` on non-zero exits.

---

## Context

### Background
Loops currently launches inner loops via a configured command, but there is no concrete inner loop runner to execute Codex and persist session metadata. The design doc defines a single unified prompt and error handling expectations.

### Current State
- `loops/outer_loop.py` launches an inner loop command and sets `LOOPS_RUN_DIR`/task env vars.
- `loops/run_record.py` provides read/write helpers for `run.json`.
- No inner loop runner implementation or tests exist.

### Constraints
- Python implementation only.
- Follow `DESIGN.md` prompt and error handling guidance.
- Avoid new external dependencies.

---

## Technical Approach

### Architecture/Design
- Add `loops/inner_loop.py` with a `run_inner_loop` function and a minimal CLI entrypoint.
- Build a unified prompt from `DESIGN.md` and the task URL from `run.json`.
- Execute Codex via `CODEX_CMD` (default `codex exec --yolo`) and capture stdout/stderr.
- Append Codex output to `run.log` and parse a session id from the output.
- Update `run.json` with `codex_session.id` and `last_prompt`; set `needs_user_input` when Codex exits non-zero.

### Technology Stack
- Python 3
- pytest

### Integration Points
- `loops.run_record.read_run_record` / `write_run_record`
- `.loops/<run>/run.log`
- `LOOPS_RUN_DIR` env var set by the outer loop launcher

### Design Patterns
- Small helpers for prompt construction and session id extraction.

### Important Context
- Unified prompt from `DESIGN.md`:
  "Use dev.do to implement the task, open a PR, wait for review, address feedback, and cleanup when approved.\nTask: [task]"
- Prior art: `/Users/kevinlin/code/skills/active/dev.watch/scripts/loops.sh` for Codex invocation.

---

## Steps

### Phase 1: Implement runner + tests
- [x] Add `loops/inner_loop.py` with prompt construction, Codex invocation, logging, and `run.json` updates.
- [x] Provide CLI entrypoint (env `LOOPS_RUN_DIR` and optional `--run-dir`).
- [x] Add tests for session id persistence and log output.
- [x] Add test for non-zero Codex exit setting `needs_user_input=true`.

---

## Testing

- `python -m pytest`

---

## Dependencies

### External Services/APIs
- None

### Libraries/Packages
- None

### Tools/Infrastructure
- Codex CLI (invoked via `CODEX_CMD`)

### Access Required
- [ ] None

---

## Risks & Mitigations

| Risk | Impact | Probability | Mitigation Strategy |
|------|--------|-------------|---------------------|
| Codex output format changes break session id parsing | Med | Med | Parse UUID patterns and log warning when missing |
| Missing prompt file expectations | Low | Med | Use inline unified prompt; optional prompt file via env |

---

## Questions

### Technical Decisions Needed
- [x] How to locate the prompt? Use inline unified prompt; allow optional prompt file via env if provided.

### Clarifications Required
- [x] None.

### Research Tasks
- [x] None.

---

## Success Criteria

- [x] `run.json` records `codex_session.id` after Codex execution.
- [x] `run.log` contains Codex output.
- [x] Non-zero exit sets `needs_user_input=true`.
- [x] Tests cover success and failure cases.

---

## Notes

- Keep the runner lightweight and compatible with the existing inner loop command launcher.
- Combined implementation + tests into a single phase to simplify execution and commits.
