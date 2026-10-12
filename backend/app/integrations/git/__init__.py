"""Provider-agnostic Git access. Import from here, not from the provider modules."""

from app.integrations.git.base import (
    AccessDeniedError,
    AuthenticationError,
    Commit,
    CompareResult,
    FileDiff,
    GitProvider,
    GitProviderError,
    Identity,
    NotFoundError,
    RateLimitError,
    Ref,
    RefKind,
    RepositoryInfo,
    UnsupportedProviderError,
)
from app.integrations.git.factory import build_provider, create_provider
from app.integrations.git.github import GitHubProvider
from app.integrations.git.gitlab import GitLabProvider

__all__ = [
    "AccessDeniedError",
    "AuthenticationError",
    "Commit",
    "CompareResult",
    "FileDiff",
    "GitHubProvider",
    "GitLabProvider",
    "GitProvider",
    "GitProviderError",
    "Identity",
    "NotFoundError",
    "RateLimitError",
    "Ref",
    "RefKind",
    "RepositoryInfo",
    "UnsupportedProviderError",
    "build_provider",
    "create_provider",
]
