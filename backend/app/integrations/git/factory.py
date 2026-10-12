"""Build a ``GitProvider`` from a stored ``GitConnection``."""

from collections.abc import Callable
from typing import Any

from app.core.crypto import decrypt_secret
from app.domain.enums import GitAuthType, GitProviderKind
from app.integrations.git.base import GitProvider, UnsupportedProviderError
from app.integrations.git.github import GITHUB_CLOUD_API, GitHubProvider
from app.integrations.git.gitlab import GITLAB_CLOUD_API, GitLabProvider
from app.models.git import GitConnection

DEFAULT_BASE_URLS: dict[str, str] = {
    GitProviderKind.GITHUB: GITHUB_CLOUD_API,
    GitProviderKind.GITLAB: GITLAB_CLOUD_API,
}


def create_provider(
    provider: str, token: str, base_url: str | None = None, **kwargs: Any
) -> GitProvider:
    """Instantiate a provider client. ``base_url`` is the provider API root (self-hosted)."""
    url = base_url or DEFAULT_BASE_URLS.get(provider)
    if provider == GitProviderKind.GITHUB:
        return GitHubProvider(token, base_url=url or GITHUB_CLOUD_API, **kwargs)
    if provider == GitProviderKind.GITLAB:
        return GitLabProvider(token, base_url=url or GITLAB_CLOUD_API, **kwargs)
    raise UnsupportedProviderError(f"git provider {provider!r} is not supported yet")


def build_provider(
    connection: GitConnection,
    *,
    decrypt: Callable[[str], str] = decrypt_secret,
    **kwargs: Any,
) -> GitProvider:
    """Decrypt the connection's access token and return a ready-to-use provider client."""
    if connection.auth_type != GitAuthType.ACCESS_TOKEN:
        raise UnsupportedProviderError(
            f"auth type {connection.auth_type!r} is not supported yet (access tokens only)"
        )
    if not connection.access_token_encrypted:
        raise UnsupportedProviderError("git connection has no access token")
    token = decrypt(connection.access_token_encrypted)
    return create_provider(connection.provider, token, connection.base_url, **kwargs)
