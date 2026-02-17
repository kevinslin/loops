import pytest

from loops.providers.github_projects_v2 import (
    GITHUB_PROJECTS_V2_PROVIDER_ID,
    GithubProjectsV2TaskProviderConfig,
)
from loops.providers.registry import get_provider_definition


def test_get_provider_definition_returns_github_provider() -> None:
    definition = get_provider_definition(GITHUB_PROJECTS_V2_PROVIDER_ID)

    assert definition.metadata.id == GITHUB_PROJECTS_V2_PROVIDER_ID
    assert definition.metadata.provider_config_model is GithubProjectsV2TaskProviderConfig
    assert definition.metadata.required_secrets[0].name == "GITHUB_TOKEN"
    assert definition.metadata.required_secrets[0].alias == ("GH_TOKEN",)


def test_get_provider_definition_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError, match="Unsupported provider_id"):
        get_provider_definition("unknown")
