# Execution Plan: Provider metadata + required secrets validation

**Date:** 2026-02-16
**Status:** Complete

---

## Goal

Introduce provider-declared metadata (`LoopsProviderConfig`) with:

- stable provider identity (`id`, optional `name`)
- required secret declarations (`required_secrets`) for preflight validation
- strongly typed provider-specific configuration via provider-owned Pydantic model

Loops should validate required secrets from environment variables only (no secret resolver/injection yet), then construct providers using validated typed config.

---

## Context

### Background

Current provider wiring in `loops/outer_loop.py` builds providers from generic `provider_config` dictionaries. Provider-specific secret handling is embedded in provider code (for example, GitHub token lookup in `loops/providers/github_projects_v2.py`), and there is no shared provider metadata contract for required secrets and typed config schemas.

### Current State

- Provider interface: `TaskProvider.poll(limit)` in `loops/task_provider.py`.
- Provider construction: `build_provider(config)` in `loops/outer_loop.py`.
- Config loading: `load_config(...)` currently validates generic `provider_config` shape.
- GitHub provider has a dataclass config and internal token lookup.

### Constraints

- Keep secret handling simple for now: required secrets are validated from process env only.
- `required_secrets` is validation/documentation metadata only; no value resolution/injection layer in this phase.
- Preserve current provider behavior while migrating to typed config.
- Keep implementation Python-only and testable with pytest.

---

## Technical Approach

### Architecture/Design

Add provider metadata and typed config wiring:

1. **Provider metadata types**
   - Add `SecretRequirement` and `LoopsProviderConfig` in a shared module (for example `loops/provider_types.py`).
   - Proposed shape:
     - `SecretRequirement`: `name`, `description`
     - `LoopsProviderConfig`: `id`, optional `name`, `required_secrets`, `provider_config_model`
   - `provider_config_model` is a Pydantic model class supplied by each provider.

2. **Provider registry**
   - Introduce a registry mapping `provider_id -> provider definition` (metadata + constructor).
   - Each provider module exports its `LOOPS_PROVIDER_CONFIG`.

3. **Typed provider config**
   - Provider defines a Pydantic model for its custom config.
   - Loops validates raw `provider_config` via `model_validate(...)`.
   - Unknown fields and invalid types become deterministic validation errors.

4. **Required secret preflight checks**
   - Before provider creation, Loops checks each `required_secrets[*].name` in `os.environ`.
   - Missing/empty env values fail fast with a clear, non-secret error.
   - No secret values are logged.

5. **Provider construction**
   - Use validated typed model values to instantiate provider runtime config.
   - Provider may still read env directly at runtime if needed; preflight ensures required values are present.

### Technology Stack

- Python 3
- Pydantic (new dependency)
- pytest

### Integration Points

- `loops/outer_loop.py`: provider config validation, secret preflight, provider construction.
- `loops/providers/github_projects_v2.py`: provider metadata + provider config Pydantic model.
- `loops/task_provider.py`: unchanged protocol usage.
- `README.md` and `DESIGN.md`: update config/secret provider documentation.

### Design Patterns

- Provider metadata contract + central registry.
- Schema-first provider config validation (Pydantic).
- Fail-fast startup checks for missing required env secrets.

### Important Context

- This phase intentionally avoids resolver complexity (dotenv/op/etc.).
- `required_secrets` validates env presence only; value plumbing remains unchanged.

---

## Data Model

### Secret requirement metadata

```python
class SecretRequirement(BaseModel):
    name: str
    alias: tuple[str, ...] = ()
    description: str
```

### Provider metadata

```python
@dataclass(frozen=True)
class LoopsProviderConfig:
    id: str
    name: str | None = None
    required_secrets: tuple[SecretRequirement, ...] = ()
    provider_config_model: type[BaseModel]
```

### Example provider declaration (GitHub)

- `id`: `github_projects_v2`
- `required_secrets`: includes `GITHUB_TOKEN` (or chosen canonical env var name)
- `provider_config_model`: provider-owned Pydantic model for `url`, `status_field`, `page_size`

---

## Validation and Error Handling

### Required secret check

- Rule: each required secret name must exist in environment and be non-empty.
- Error format:
  - provider id/name
  - missing secret name(s)
  - description(s)
- Never print secret values.

### Provider config check

- Validate raw `provider_config` via provider’s Pydantic model.
- Surface concise validation failures (missing fields, wrong types, extra fields).

---

## Steps

### Phase 1: Add provider metadata primitives

- [x] Add shared `SecretRequirement` and `LoopsProviderConfig` types.
- [x] Add provider definition/registry wiring (`provider_id` lookup by metadata id).

### Phase 2: Add Pydantic provider config models

- [x] Define Pydantic config model for GitHub provider.
- [x] Connect model validation to provider construction path.
- [x] Preserve current defaults (`status_field`, `page_size`) in typed model.

### Phase 3: Required secrets preflight

- [x] Add env presence validation for provider-declared `required_secrets`.
- [x] Fail before provider polling when required secrets are missing/empty.
- [x] Keep error messages descriptive and secret-safe.

### Phase 4: Provider migration and cleanup

- [x] Migrate GitHub provider declaration to `LoopsProviderConfig`.
- [x] Remove redundant generic provider config filtering logic superseded by model validation.
- [x] Keep backward compatibility behavior only where necessary.

### Phase 5: Tests and docs

- [x] Add tests for metadata registry and typed model validation.
- [x] Add tests for missing required env secret failures.
- [x] Update `DESIGN.md` and `README.md` with metadata + env-secret check behavior.
- [x] Run full test suite.

**Dependencies between phases:**

- Phase 2 depends on Phase 1 metadata/registry.
- Phase 3 depends on Phase 1 registry.
- Phase 4 depends on Phases 2-3.
- Phase 5 depends on all prior phases.

---

## Testing

- `python -m pytest tests/test_github_projects_v2_provider.py`
- `python -m pytest tests/test_outer_loop.py tests/test_cli.py`
- `python -m pytest`

New/updated coverage targets:

- provider metadata contract and registry lookup
- Pydantic validation errors for invalid `provider_config`
- missing/empty required env secret handling
- secret-safe error output (no values)

---

## Risks & Mitigations

| Risk | Impact | Probability | Mitigation Strategy |
|------|--------|-------------|---------------------|
| Migration breaks existing configs | Med | Med | Preserve field defaults and add clear validation errors |
| Secret preflight conflicts with provider runtime behavior | Low | Med | Keep checks as presence-only; provider runtime semantics remain intact |
| Over-tight validation blocks previously accepted configs | Med | Med | Document required fields and include migration notes in README |
| Pydantic dependency mismatch | Low | Low | Pin and test dependency in CI/local workflows |

---

## Questions

### Technical Decisions Needed

- [x] Canonical env var names per provider secret (for example only `GITHUB_TOKEN` vs aliases). 
   - Should also allow aliases. Create an `alias` field in `SecretRequirement`
- [x] Whether to allow provider-specific alias fallback in preflight checks. 
   - Yes

### Clarifications Required

- [x] None beyond canonical env-var naming decision.

### Research Tasks

- [x] None.

---

## Success Criteria

- [x] Providers declare `LoopsProviderConfig` with `id`, optional `name`, `required_secrets`, and provider-owned Pydantic config model.
- [x] Loops validates provider custom config using the provider’s typed model.
- [x] Loops validates required secret presence from env before provider polling.
- [x] Missing secrets fail fast with actionable, secret-safe messages.
- [x] Full tests pass.

---

## Notes

- This spec intentionally keeps secret handling minimal (env-only validation).
- Future specs can add pluggable secret resolvers (dotenv, 1Password CLI, etc.) without changing provider metadata shape.
