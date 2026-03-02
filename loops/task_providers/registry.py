from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from pydantic import BaseModel

from loops.state.provider_types import LoopsProviderConfig
from loops.task_providers.github_projects_v2 import (
    GITHUB_PROJECTS_V2_PROVIDER_CONFIG,
    GITHUB_PROJECTS_V2_PROVIDER_ID,
    GithubProjectsV2TaskProvider,
    GithubProjectsV2TaskProviderConfig,
)
from loops.task_providers.base import TaskProvider


@dataclass(frozen=True)
class ProviderDefinition:
    """Provider construction metadata and runtime factory."""

    metadata: LoopsProviderConfig
    build: Callable[[BaseModel], TaskProvider]


def _build_github_projects_v2_provider(config_model: BaseModel) -> TaskProvider:
    if not isinstance(config_model, GithubProjectsV2TaskProviderConfig):
        raise TypeError(
            "github_projects_v2 config model must be GithubProjectsV2TaskProviderConfig"
        )
    return GithubProjectsV2TaskProvider(config_model)


_PROVIDERS: dict[str, ProviderDefinition] = {
    GITHUB_PROJECTS_V2_PROVIDER_ID: ProviderDefinition(
        metadata=GITHUB_PROJECTS_V2_PROVIDER_CONFIG,
        build=_build_github_projects_v2_provider,
    ),
}


def get_provider_definition(provider_id: str) -> ProviderDefinition:
    """Return provider definition by id."""

    provider = _PROVIDERS.get(provider_id)
    if provider is None:
        raise ValueError(f"Unsupported provider_id: {provider_id}")
    return provider
