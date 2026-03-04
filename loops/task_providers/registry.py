from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

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
    build: Callable[[BaseModel, Mapping[str, str]], TaskProvider]


def _build_github_projects_v2_provider(
    config_model: BaseModel,
    environ: Mapping[str, str],
) -> TaskProvider:
    if not isinstance(config_model, GithubProjectsV2TaskProviderConfig):
        raise TypeError(
            "github_projects_v2 config model must be GithubProjectsV2TaskProviderConfig"
        )
    runtime_token = environ.get("GITHUB_TOKEN") or environ.get("GH_TOKEN")
    if config_model.github_token is None and runtime_token and runtime_token.strip():
        config_model = config_model.model_copy(update={"github_token": runtime_token})
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
        supported = ", ".join(sorted(_PROVIDERS))
        raise ValueError(
            f"Unsupported provider_id: {provider_id}. Supported provider_ids: {supported}"
        )
    return provider
