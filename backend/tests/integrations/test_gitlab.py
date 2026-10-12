"""GitLab-specific behaviour."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest

from app.domain.enums import ChangeType
from app.integrations.git import GitLabProvider, RefKind
from app.integrations.git.gitlab import COMPARE_FILE_CAP
from tests.integrations import fakes

PROJECT = "/api/v4/projects/acme%2Fplatform%2Fapi"


@pytest.fixture
async def clients() -> AsyncIterator[list[httpx.AsyncClient]]:
    created: list[httpx.AsyncClient] = []
    yield created
    for client in created:
        await client.aclose()


def make(clients: list[httpx.AsyncClient], handler: fakes.Handler) -> GitLabProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    clients.append(client)
    return GitLabProvider(fakes.TOKEN, fakes.GITLAB_API, client=client)


async def test_nested_group_path_is_url_encoded(clients: list[httpx.AsyncClient]) -> None:
    recorder = fakes.Recorder()
    await make(clients, fakes.gitlab_handler(recorder)).compare(
        fakes.GITLAB_REPO, fakes.BASE_SHA, fakes.HEAD_SHA
    )

    assert recorder.paths()[0] == f"{PROJECT}/repository/compare"
    assert recorder.requests[0].headers["private-token"] == fakes.TOKEN


async def test_compare_timeout_sets_truncated(clients: list[httpx.AsyncClient]) -> None:
    handler = fakes.gitlab_handler(fakes.Recorder(), compare_timeout=True)
    result = await make(clients, handler).compare(fakes.GITLAB_REPO, fakes.BASE_SHA, fakes.HEAD_SHA)
    assert result.truncated is True


async def test_diff_overflow_sets_truncated(clients: list[httpx.AsyncClient]) -> None:
    diffs = [
        {"old_path": f"f{i}", "new_path": f"f{i}", "diff": "@@ -1 +1 @@\n-a\n+b\n"}
        for i in range(COMPARE_FILE_CAP)
    ]
    handler = fakes.gitlab_handler(fakes.Recorder(), diffs=diffs)
    result = await make(clients, handler).compare(fakes.GITLAB_REPO, fakes.BASE_SHA, fakes.HEAD_SHA)
    assert result.truncated is True


async def test_too_large_diff_is_rebuilt(clients: list[httpx.AsyncClient]) -> None:
    diffs = [{"old_path": fakes.BIG_FILE, "new_path": fakes.BIG_FILE, "diff": "",
              "too_large": True}]  # fmt: skip
    handler = fakes.gitlab_handler(fakes.Recorder(), diffs=diffs)
    result = await make(clients, handler).compare(fakes.GITLAB_REPO, fakes.BASE_SHA, fakes.HEAD_SHA)
    assert result.files[0].patch is not None
    assert (result.files[0].additions, result.files[0].deletions) == (1, 1)


async def test_diff_with_file_headers_is_normalized(clients: list[httpx.AsyncClient]) -> None:
    """Older GitLab versions prefix ``diff`` with ``---``/``+++`` file headers."""
    text = "--- a/x.sql\n+++ b/x.sql\n@@ -1,2 +1,2 @@\n--- old comment\n+-- new comment\n keep\n"
    diffs = [{"old_path": "x.sql", "new_path": "x.sql", "diff": text}]
    handler = fakes.gitlab_handler(fakes.Recorder(), diffs=diffs)
    result = await make(clients, handler).compare(fakes.GITLAB_REPO, fakes.BASE_SHA, fakes.HEAD_SHA)
    file = result.files[0]

    assert file.patch == "@@ -1,2 +1,2 @@\n--- old comment\n+-- new comment\n keep\n"
    assert (file.additions, file.deletions) == (1, 1)
    assert file.status is ChangeType.MODIFIED


async def test_verify_credentials_with_token_expiry(clients: list[httpx.AsyncClient]) -> None:
    routes: dict[str, fakes.Handler] = {
        "/api/v4/user": fakes.json_route({"username": "dev", "name": "Dev One"}),
        "/api/v4/personal_access_tokens/self": fakes.json_route({"expires_at": "2026-12-31"}),
    }
    identity = await make(
        clients, fakes.gitlab_handler(fakes.Recorder(), extra=routes)
    ).verify_credentials()

    assert identity.username == "dev"
    assert identity.token_expires_at == datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)


async def test_verify_credentials_without_token_endpoint(
    clients: list[httpx.AsyncClient],
) -> None:
    routes: dict[str, fakes.Handler] = {"/api/v4/user": fakes.json_route({"username": "dev"})}
    provider = make(clients, fakes.gitlab_handler(fakes.Recorder(), extra=routes))
    identity = await provider.verify_credentials()

    assert identity.username == "dev"
    assert identity.token_expires_at is None


async def test_resolve_ref(clients: list[httpx.AsyncClient]) -> None:
    routes: dict[str, fakes.Handler] = {
        f"{PROJECT}/repository/commits/release%2F1.2": fakes.json_route({"id": fakes.HEAD_SHA})
    }
    provider = make(clients, fakes.gitlab_handler(fakes.Recorder(), extra=routes))
    assert await provider.resolve_ref(fakes.GITLAB_REPO, "release/1.2") == fakes.HEAD_SHA


async def test_get_repository(clients: list[httpx.AsyncClient]) -> None:
    routes: dict[str, fakes.Handler] = {
        PROJECT: fakes.json_route(
            {"id": 7, "path_with_namespace": fakes.GITLAB_REPO, "default_branch": None}
        )
    }
    info = await make(clients, fakes.gitlab_handler(fakes.Recorder(), extra=routes)).get_repository(
        fakes.GITLAB_REPO
    )
    assert (info.external_id, info.full_name, info.default_branch) == (
        "7",
        fakes.GITLAB_REPO,
        "main",
    )


async def test_list_refs_uses_search(clients: list[httpx.AsyncClient]) -> None:
    recorder = fakes.Recorder()
    routes: dict[str, fakes.Handler] = {
        f"{PROJECT}/repository/branches": fakes.json_route(
            [{"name": "release/1.2", "commit": {"id": "1" * 40}}]
        ),
        f"{PROJECT}/repository/tags": fakes.json_route(
            [{"name": "v1.2.0", "commit": {"id": "2" * 40}}]
        ),
    }
    refs = await make(clients, fakes.gitlab_handler(recorder, extra=routes)).list_refs(
        fakes.GITLAB_REPO, "rel", limit=5
    )

    assert [(r.name, r.kind) for r in refs] == [
        ("release/1.2", RefKind.BRANCH),
        ("v1.2.0", RefKind.TAG),
    ]
    assert all(dict(r.url.params) == {"search": "rel", "per_page": "5"} for r in recorder.requests)


async def test_rejects_empty_project(clients: list[httpx.AsyncClient]) -> None:
    with pytest.raises(ValueError):
        await make(clients, fakes.gitlab_handler(fakes.Recorder())).get_repository("/")
