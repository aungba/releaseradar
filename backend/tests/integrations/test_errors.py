"""HTTP error mapping, shared by every provider."""

from collections.abc import AsyncIterator, Awaitable, Callable

import httpx
import pytest

from app.integrations.git import (
    AccessDeniedError,
    AuthenticationError,
    GitHubProvider,
    GitLabProvider,
    GitProviderError,
    NotFoundError,
    RateLimitError,
)
from tests.integrations import fakes

PROVIDERS: dict[str, tuple[Callable[..., GitHubProvider | GitLabProvider], str, str]] = {
    "github": (GitHubProvider, fakes.GITHUB_API, fakes.GITHUB_REPO),
    "gitlab": (GitLabProvider, fakes.GITLAB_API, fakes.GITLAB_REPO),
}

Responder = Callable[[httpx.Request], httpx.Response]
Call = Callable[[Responder], Awaitable[None]]


@pytest.fixture(params=list(PROVIDERS))
async def call(
    request: pytest.FixtureRequest,
) -> AsyncIterator[Call]:
    """``await call(responder)`` runs ``get_repository`` against a fake server."""
    cls, base_url, repo = PROVIDERS[request.param]
    clients: list[httpx.AsyncClient] = []

    async def run(responder: Responder) -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(responder))
        clients.append(client)
        provider = cls(fakes.TOKEN, base_url, client=client)
        await provider.get_repository(repo)

    yield run
    for client in clients:
        await client.aclose()


def respond(status: int, headers: dict[str, str] | None = None) -> Responder:
    return lambda _r: httpx.Response(status, json={"message": "nope"}, headers=headers)


@pytest.mark.parametrize(
    ("status", "headers", "error_type"),
    [
        (401, None, AuthenticationError),
        (403, None, AccessDeniedError),
        (403, {"x-ratelimit-remaining": "0"}, RateLimitError),
        (403, {"retry-after": "60"}, RateLimitError),
        (429, None, RateLimitError),
        (404, None, NotFoundError),
        (500, None, GitProviderError),
        (502, None, GitProviderError),
    ],
)
async def test_status_maps_to_typed_error(
    call: Call,
    status: int,
    headers: dict[str, str] | None,
    error_type: type[GitProviderError],
) -> None:
    with pytest.raises(error_type) as excinfo:
        await call(respond(status, headers))

    assert type(excinfo.value) is error_type
    assert excinfo.value.status_code == status
    assert fakes.TOKEN not in str(excinfo.value)
    assert fakes.TOKEN not in repr(excinfo.value)


async def test_retry_after_is_parsed(call: Call) -> None:
    with pytest.raises(RateLimitError) as excinfo:
        await call(respond(429, {"retry-after": "30"}))
    assert excinfo.value.retry_after_seconds == 30


async def test_network_error_is_wrapped(call: Call) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(GitProviderError) as excinfo:
        await call(boom)
    assert excinfo.value.status_code is None
    assert fakes.TOKEN not in str(excinfo.value)


async def test_timeout_is_wrapped(call: Call) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(GitProviderError, match="timed out"):
        await call(slow)


async def test_owned_client_is_closed() -> None:
    provider = GitHubProvider(fakes.TOKEN)
    async with provider:
        pass
    assert provider._client.is_closed


async def test_injected_client_is_not_closed() -> None:
    async with httpx.AsyncClient() as client:
        async with GitLabProvider(fakes.TOKEN, client=client):
            pass
        assert not client.is_closed
