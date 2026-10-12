"""GitHub-specific behaviour."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest

from app.integrations.git import GitHubProvider, NotFoundError, RefKind
from app.integrations.git.github import COMPARE_FILE_CAP, parse_token_expiration
from tests.integrations import fakes

REPO_PATH = f"/api/v3/repos/{fakes.GITHUB_REPO}"


@pytest.fixture
async def client_factory() -> AsyncIterator[list[httpx.AsyncClient]]:
    clients: list[httpx.AsyncClient] = []
    yield clients
    for client in clients:
        await client.aclose()


def make(clients: list[httpx.AsyncClient], handler: fakes.Handler) -> GitHubProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    clients.append(client)
    return GitHubProvider(fakes.TOKEN, fakes.GITHUB_API, client=client)


async def test_compare_follows_pagination(client_factory: list[httpx.AsyncClient]) -> None:
    recorder = fakes.Recorder()
    provider = make(client_factory, fakes.github_handler(recorder))
    result = await provider.compare(fakes.GITHUB_REPO, fakes.BASE_SHA, fakes.HEAD_SHA)

    compare_requests = [r for r in recorder.requests if "/compare/" in r.url.path]
    assert len(compare_requests) == 2
    assert dict(compare_requests[0].url.params) == {"per_page": "100"}
    assert compare_requests[1].url.params["page"] == "2"
    assert len(result.commits) == 3
    assert len(result.files) == 7  # files repeated on page 2 are not duplicated


async def test_compare_file_cap_sets_truncated(client_factory: list[httpx.AsyncClient]) -> None:
    files = [
        {"filename": f"f{i}.py", "status": "modified", "additions": 1, "deletions": 0,
         "changes": 1, "patch": "@@ -0,0 +1 @@\n+x\n"}
        for i in range(COMPARE_FILE_CAP)
    ]  # fmt: skip
    provider = make(client_factory, fakes.github_handler(fakes.Recorder(), files=files))
    result = await provider.compare(fakes.GITHUB_REPO, fakes.BASE_SHA, fakes.HEAD_SHA)

    assert len(result.files) == COMPARE_FILE_CAP
    assert result.truncated is True


async def test_missing_commits_sets_truncated(client_factory: list[httpx.AsyncClient]) -> None:
    handler = fakes.github_handler(fakes.Recorder(), total_commits=500)
    result = await make(client_factory, handler).compare(
        fakes.GITHUB_REPO, fakes.BASE_SHA, fakes.HEAD_SHA
    )
    assert result.truncated is True


async def test_resolve_ref_returns_sha(client_factory: list[httpx.AsyncClient]) -> None:
    recorder = fakes.Recorder()
    routes: dict[str, fakes.Handler] = {
        f"{REPO_PATH}/commits/release%2F1.2": lambda _r: httpx.Response(
            200, text=fakes.HEAD_SHA + "\n"
        )
    }
    provider = make(client_factory, fakes.github_handler(recorder, extra=routes))

    assert await provider.resolve_ref(fakes.GITHUB_REPO, "release/1.2") == fakes.HEAD_SHA
    assert recorder.requests[-1].headers["accept"] == "application/vnd.github.sha"


async def test_resolve_ref_422_is_not_found(client_factory: list[httpx.AsyncClient]) -> None:
    routes: dict[str, fakes.Handler] = {
        f"{REPO_PATH}/commits/deadbeef": fakes.json_route({"message": "No commit found"}, 422)
    }
    provider = make(client_factory, fakes.github_handler(fakes.Recorder(), extra=routes))

    with pytest.raises(NotFoundError):
        await provider.resolve_ref(fakes.GITHUB_REPO, "deadbeef")


async def test_verify_credentials_reads_token_expiry(
    client_factory: list[httpx.AsyncClient],
) -> None:
    routes: dict[str, fakes.Handler] = {
        "/api/v3/user": fakes.json_route(
            {"login": "octocat", "name": "The Octocat"},
            headers={"github-authentication-token-expiration": "2026-11-11 08:00:00 UTC"},
        )
    }
    provider = make(client_factory, fakes.github_handler(fakes.Recorder(), extra=routes))
    identity = await provider.verify_credentials()

    assert identity.username == "octocat"
    assert identity.display_name == "The Octocat"
    assert identity.token_expires_at == datetime(2026, 11, 11, 8, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-11-11 08:00:00 UTC", datetime(2026, 11, 11, 8, tzinfo=UTC)),
        ("2026-11-11 08:00:00 +0000", datetime(2026, 11, 11, 8, tzinfo=UTC)),
        (None, None),
        ("", None),
        ("not a date", None),
    ],
)
def test_parse_token_expiration(value: str | None, expected: datetime | None) -> None:
    assert parse_token_expiration(value) == expected


async def test_get_repository(client_factory: list[httpx.AsyncClient]) -> None:
    routes: dict[str, fakes.Handler] = {
        REPO_PATH: fakes.json_route({"id": 42, "full_name": "acme/api", "default_branch": "dev"})
    }
    provider = make(client_factory, fakes.github_handler(fakes.Recorder(), extra=routes))
    info = await provider.get_repository(fakes.GITHUB_REPO)

    assert (info.external_id, info.full_name, info.default_branch) == ("42", "acme/api", "dev")


async def test_list_refs_branches_and_annotated_tags(
    client_factory: list[httpx.AsyncClient],
) -> None:
    routes: dict[str, fakes.Handler] = {
        f"{REPO_PATH}/git/matching-refs/heads/rel": fakes.json_route(
            [{"ref": "refs/heads/release/1.2", "object": {"type": "commit", "sha": "1" * 40}}]
        ),
        f"{REPO_PATH}/git/matching-refs/tags/rel": fakes.json_route(
            [
                {"ref": "refs/tags/rel-light", "object": {"type": "commit", "sha": "2" * 40}},
                {"ref": "refs/tags/rel-annotated", "object": {"type": "tag", "sha": "3" * 40}},
            ]
        ),
    }
    provider = make(client_factory, fakes.github_handler(fakes.Recorder(), extra=routes))
    refs = await provider.list_refs(fakes.GITHUB_REPO, "rel")

    assert [(r.name, r.kind, r.sha) for r in refs] == [
        ("release/1.2", RefKind.BRANCH, "1" * 40),
        ("rel-light", RefKind.TAG, "2" * 40),
        ("rel-annotated", RefKind.TAG, None),
    ]
    assert len(await provider.list_refs(fakes.GITHUB_REPO, "rel", limit=1)) == 1


@pytest.mark.parametrize("repo", ["acme", "acme/api/extra", "/api", "acme/"])
async def test_rejects_malformed_repo(client_factory: list[httpx.AsyncClient], repo: str) -> None:
    provider = make(client_factory, fakes.github_handler(fakes.Recorder()))
    with pytest.raises(ValueError):
        await provider.get_repository(repo)


async def test_binary_detected_when_rebuilt_content_is_binary(
    client_factory: list[httpx.AsyncClient],
) -> None:
    """A large diff whose raw content turns out to be binary is flagged, not omitted."""
    files = [{"filename": "blob.dat", "status": "modified", "additions": 3, "deletions": 3,
              "changes": 6}]  # fmt: skip
    routes: dict[str, fakes.Handler] = {
        f"{REPO_PATH}/contents/blob.dat": lambda _r: httpx.Response(200, content=b"\x00\x01")
    }
    handler = fakes.github_handler(fakes.Recorder(), files=files, extra=routes)
    result = await make(client_factory, handler).compare(
        fakes.GITHUB_REPO, fakes.BASE_SHA, fakes.HEAD_SHA
    )

    assert result.files[0].is_binary is True
    assert result.files[0].patch_omitted is False
