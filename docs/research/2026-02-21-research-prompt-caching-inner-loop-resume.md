# Research Brief: Prompt Caching and Inner-Loop Session Reuse

**Last Updated**: 2026-02-21

**Status**: Complete

**Related**:

- `DESIGN.md`
- `docs/flows/ref.inner-loop.md`
- `docs/specs/2026-02-09-manage-inner-loop-state-machine.md`

* * *

## Executive Summary

This research focuses on how prompt-caching practices should shape Loops inner-loop behavior. The primary trigger was Thariq's article ("Lessons from Building Claude Code: Prompt Caching Is Everything"), which emphasizes that long-running agent systems are only economical when prompt prefixes remain stable and cache-hit rate is protected as an operational metric.

The article guidance maps directly to this repo's issue (`loops/TODO.md`): the inner loop currently starts a fresh Codex session for each turn, which increases cache misses, latency, and cost risk. The recommended direction is to preserve and reuse the same Codex session whenever available, while keeping prompt/tool/model configuration stable inside a run.

**Research Questions**:

1. What concrete prompt-caching practices from the article apply to Loops inner-loop design?
2. How should those practices be translated into implementation constraints for Codex session lifecycle?
3. Which observability signals should be added to ensure cache-friendly behavior is sustained?

* * *

## Research Methodology

### Approach

- Read the user-provided article copy at `/tmp/out.md`.
- Cross-checked article claims against OpenAI prompt-caching documentation for API-level mechanics and constraints.
- Mapped findings onto current Loops inner-loop flow and run-state model.

### Sources

- User-provided article copy: `/tmp/out.md` (from original link: https://x.com/trq212/status/2024574133011673516)
- OpenAI Prompt Caching guide: https://platform.openai.com/docs/guides/prompt-caching
- OpenAI Prompt Caching product note: https://openai.com/index/api-prompt-caching/

* * *

## Research Findings

### Cache Mechanics and Prefix Stability

#### Prefix match is the main constraint

**Status**: Complete

**Details**:

- The article states prompt caching behaves as prefix matching and that prefix churn is the primary cache killer.
- OpenAI docs confirm cache hits require exact prefix matches and recommend static-first / dynamic-last prompt layout.
- This implies inner-loop prompts should keep stable instructions and tool definitions at the beginning, with volatile context appended late.

**Assessment**: Fully aligned across article and official docs.

* * *

#### Session continuity matters for agent loops

**Status**: Complete

**Details**:

- The article explicitly recommends avoiding model/tool churn and preserving session continuity.
- Loops currently stores `codex_session.id` in `run.json`, but turn execution does not use that ID to resume.
- This gap means Loops retains metadata but does not realize cache benefits from continuity.

**Assessment**: High-value, low-ambiguity improvement target.

* * *

### Tooling and Model Stability

#### Avoid toolset mutation mid-session

**Status**: Complete

**Details**:

- The article warns that adding/removing tools mid-session invalidates cacheable prefixes.
- OpenAI docs note tools are part of cacheable prompt prefix and must remain identical for reuse.
- For Loops, tool stability means avoiding mode changes that swap tool definitions between turns.

**Assessment**: Design constraint should be encoded in flow/docs and future mode architecture.

* * *

#### Avoid model switching inside the same run

**Status**: Complete

**Details**:

- The article states caches are model-specific and switching models mid-session incurs rebuild cost.
- Loops should treat model choice as run-scoped and stable once a run starts.
- If model changes are needed, they should happen via explicit handoff/new run boundaries.

**Assessment**: Important policy; easy to enforce through config discipline.

* * *

### Compaction and Forking Patterns

#### Cache-safe compaction/forking requires same prefix context

**Status**: Complete

**Details**:

- The article notes that separate compaction prompts with different system/tool prefixes cause full cache misses.
- Recommended pattern: keep same system/tool/context prefix and append compaction request as trailing message content.
- For Loops, any future summarize/compaction actions should preserve the same session and prefix shape.

**Assessment**: Future-facing but crucial for long-running runs.

* * *

## Comparative Analysis

| Criteria | Fresh session each turn | Resume same session | Resume session + stable prompt/tool/model policy |
| --- | --- | --- | --- |
| Cache hit potential | Low | Medium-High | High |
| Latency consistency | Low | Medium | High |
| Token cost efficiency | Low | Medium-High | High |
| Operational complexity | Low | Low-Medium | Medium |
| Fit with article guidance | Poor | Good | Best |

**Strengths/Weaknesses Summary**:

- **Fresh session each turn**: Simple implementation but repeatedly forfeits prefix reuse.
- **Resume same session**: Captures most value quickly with minimal architecture change.
- **Resume + policy**: Strongest long-term cost/latency profile; requires explicit guardrails and observability.

* * *

## Best Practices

1. **Preserve session continuity**: Reuse prior `codex_session.id` for follow-up turns inside the same run.
2. **Keep static prefix immutable**: Place stable instructions/tools first; append dynamic state later.
3. **Do not mutate tools mid-run**: Prefer mode signals/messages over toolset replacement.
4. **Do not switch models mid-run**: Treat model choice as run-scoped unless intentionally forking.
5. **Track cache health operationally**: Monitor cache-hit proxies (or direct cached-token metrics when available) as a reliability signal.

* * *

## Open Research Questions

1. **Codex CLI resume semantics**: Which resume invocation is most robust (`resume <id>` vs equivalent flags) across environments?
2. **Fallback behavior**: What exact retry/fallback path should Loops take if resume fails for a stale/invalid session ID?
3. **Telemetry surface**: Which Codex/usage fields are reliably available in Loops logs to estimate cache effectiveness per run?

* * *

## Recommendations

### Summary

Implement session reuse now, and enforce stable prompt/tool/model behavior as explicit run-level invariants.

### Recommended Approach

- Update inner-loop Codex execution so:
  - First turn starts normally and captures `session_id`.
  - Subsequent turns resume using stored `run_record.codex_session.id`.
- Keep prompt structure deterministic:
  - Static base prompt + stable instructions first.
  - Dynamic state/user deltas appended later.
- Add logging around session strategy (new vs resume) and failures/fallbacks.

**Rationale**:

- Directly addresses known TODO and aligns with article's core guidance.
- Provides immediate cost/latency improvement potential.
- Keeps changes localized to `loops/inner_loop.py` and test suite.

### Alternative Approaches

- Continue fresh sessions and optimize prompt text only:
  - Lower implementation risk, but leaves major caching benefit unrealized.
- Build full prompt-compaction framework first:
  - Valuable eventually, but delays highest-impact fix.

* * *

## References

- Thariq, "Lessons from Building Claude Code: Prompt Caching Is Everything" (user-provided copy): `/tmp/out.md`
- Original post link: https://x.com/trq212/status/2024574133011673516
- OpenAI API Prompt Caching guide: https://platform.openai.com/docs/guides/prompt-caching
- OpenAI Prompt Caching product announcement: https://openai.com/index/api-prompt-caching/

* * *

## Appendices

### Appendix A: Mapping to Loops TODO

- TODO: "each codex starts a fresh session in inner loop. should re-use existing session"
- Research conclusion: adopt same-session resume as default for all non-initial Codex turns.

### Appendix B: Immediate Implementation Constraints

- Do not break first-turn behavior.
- Resume only when `codex_session.id` exists.
- If resume fails, fallback should set `needs_user_input` or retry strategy (implementation decision).

## Manual Notes 

[keep this for the user to add notes. do not change between edits]

## Changelog
- 2026-02-21: Added initial prompt-caching research brief tied to inner-loop session reuse work. (019c7f29-03fa-7572-82c6-9ea5b14ceb9c)
