from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel


@dataclass(frozen=True)
class SecretRequirement:
    """Declare an env-var secret requirement for a provider."""

    name: str
    description: str
    alias: tuple[str, ...] = field(default_factory=tuple)

    def env_names(self) -> tuple[str, ...]:
        """Return canonical + alias env names with stable de-duplication."""

        names: list[str] = []
        seen: set[str] = set()
        for candidate in (self.name, *self.alias):
            normalized = candidate.strip()
            if not normalized or normalized in seen:
                continue
            names.append(normalized)
            seen.add(normalized)
        return tuple(names)


@dataclass(frozen=True)
class LoopsProviderConfig:
    """Provider metadata used for config validation and startup checks."""

    id: str
    provider_config_model: type[BaseModel]
    name: str | None = None
    required_secrets: tuple[SecretRequirement, ...] = field(default_factory=tuple)

    def display_name(self) -> str:
        """Return a human-friendly provider name."""

        return self.name or self.id
