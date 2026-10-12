"""Provider factory and connection health."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.fernet import Fernet

from app.core.crypto import decrypt_secret, encrypt_secret
from app.domain.enums import GitAuthType, GitProviderKind
from app.integrations.git import (
    AuthenticationError,
    GitHubProvider,
    GitLabProvider,
    UnsupportedProviderError,
    build_provider,
    create_provider,
)
from app.models.git import GitConnection
from app.services.git_connections import TokenHealth, token_health, verify_connection
from tests.integrations import fakes

KEY = Fernet.generate_key().decode()
NOW = datetime(2026, 10, 12, 12, tzinfo=UTC)


def connection(**overrides: object) -> GitConnection:
    values: dict[str, object] = {
        "provider": GitProviderKind.GITHUB,
        "base_url": "https://github.example.com/api/v3",
        "auth_type": GitAuthType.ACCESS_TOKEN,
        "account_login": "acme",
        "access_token_encrypted": encrypt_secret(fakes.TOKEN, KEY),
    }
    values.update(overrides)
    return GitConnection(**values)


def decrypt(ciphertext: str) -> str:
    return decrypt_secret(ciphertext, KEY)


@pytest.mark.parametrize(
    ("provider", "base_url", "cls"),
    [
        (GitProviderKind.GITHUB, "https://github.example.com/api/v3", GitHubProvider),
        (GitProviderKind.GITLAB, "https://gitlab.example.com/api/v4", GitLabProvider),
    ],
)
async def test_build_provider_uses_connection_settings(
    provider: str, base_url: str, cls: type
) -> None:
    built = build_provider(connection(provider=provider, base_url=base_url), decrypt=decrypt)
    try:
        assert isinstance(built, cls)
        assert isinstance(built, GitHubProvider | GitLabProvider)
        assert built._base_url == base_url
        assert fakes.TOKEN in " ".join(built._headers.values())
    finally:
        await built.aclose()


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        (GitProviderKind.GITHUB, "https://api.github.com"),
        (GitProviderKind.GITLAB, "https://gitlab.com/api/v4"),
    ],
)
async def test_create_provider_defaults_to_cloud(provider: str, expected: str) -> None:
    built = create_provider(provider, "t")
    try:
        assert isinstance(built, GitHubProvider | GitLabProvider)
        assert built._base_url == expected
    finally:
        await built.aclose()


def test_bitbucket_is_not_supported_yet() -> None:
    with pytest.raises(UnsupportedProviderError):
        build_provider(connection(provider=GitProviderKind.BITBUCKET), decrypt=decrypt)


def test_app_installation_is_not_supported_yet() -> None:
    conn = connection(
        auth_type=GitAuthType.APP_INSTALLATION,
        access_token_encrypted=None,
        external_installation_id="123",
    )
    with pytest.raises(UnsupportedProviderError):
        build_provider(conn, decrypt=decrypt)


def test_missing_token_is_rejected() -> None:
    with pytest.raises(UnsupportedProviderError):
        build_provider(connection(access_token_encrypted=None), decrypt=decrypt)


# ---------------------------------------------------------------- health


@pytest.mark.parametrize(
    ("expires_at", "expected"),
    [
        (None, TokenHealth.UNKNOWN),
        (NOW - timedelta(seconds=1), TokenHealth.EXPIRED),
        (NOW, TokenHealth.EXPIRED),
        (NOW + timedelta(days=3), TokenHealth.EXPIRING_SOON),
        (NOW + timedelta(days=14), TokenHealth.EXPIRING_SOON),
        (NOW + timedelta(days=15), TokenHealth.OK),
    ],
)
def test_token_health(expires_at: datetime | None, expected: TokenHealth) -> None:
    assert token_health(expires_at, now=NOW) is expected


def _github(user_headers: dict[str, str] | None = None, status: int = 200) -> httpx.AsyncClient:
    routes: dict[str, fakes.Handler] = {
        "/api/v3/user": fakes.json_route({"login": "octocat"}, status, user_headers)
    }
    handler = fakes.github_handler(fakes.Recorder(), extra=routes)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_verify_connection_records_expiry() -> None:
    conn = connection()
    headers = {"github-authentication-token-expiration": "2026-10-20 00:00:00 UTC"}
    async with _github(headers) as client:
        identity = await verify_connection(
            conn, GitHubProvider(fakes.TOKEN, fakes.GITHUB_API, client=client), now=NOW
        )

    assert identity.username == "octocat"
    assert conn.last_verified_at == NOW
    assert conn.token_expires_at == datetime(2026, 10, 20, tzinfo=UTC)
    assert token_health(conn.token_expires_at, now=NOW) is TokenHealth.EXPIRING_SOON


async def test_verify_connection_keeps_manual_expiry_when_provider_is_silent() -> None:
    manual = datetime(2027, 1, 1, tzinfo=UTC)
    conn = connection(token_expires_at=manual)
    async with _github() as client:
        await verify_connection(
            conn, GitHubProvider(fakes.TOKEN, fakes.GITHUB_API, client=client), now=NOW
        )
    assert conn.token_expires_at == manual


async def test_verify_connection_propagates_auth_failure() -> None:
    conn = connection()
    async with _github(status=401) as client:
        with pytest.raises(AuthenticationError):
            await verify_connection(
                conn, GitHubProvider(fakes.TOKEN, fakes.GITHUB_API, client=client), now=NOW
            )
    assert conn.last_verified_at is None
