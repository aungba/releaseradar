"""Shared contract suite: every provider must normalize the same scenario identically."""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from app.domain.enums import ChangeType
from app.integrations.git import (
    CompareResult,
    FileDiff,
    GitHubProvider,
    GitLabProvider,
    GitProvider,
    NotFoundError,
)
from app.integrations.git.diffutil import build_patch
from tests.integrations import fakes


class Harness:
    """Builds a provider for one host, wired to that host's fake API."""

    def __init__(self, host: str) -> None:
        self.host = host
        self.repo = fakes.GITHUB_REPO if host == "github" else fakes.GITLAB_REPO
        self.recorder = fakes.Recorder()
        self._clients: list[httpx.AsyncClient] = []

    def provider(self, **kwargs: Any) -> GitProvider:
        if self.host == "github":
            handler = fakes.github_handler(self.recorder)
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            self._clients.append(client)
            return GitHubProvider(fakes.TOKEN, fakes.GITHUB_API, client=client, **kwargs)
        handler = fakes.gitlab_handler(self.recorder)
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self._clients.append(client)
        return GitLabProvider(fakes.TOKEN, fakes.GITLAB_API, client=client, **kwargs)

    async def compare(self, **kwargs: Any) -> CompareResult:
        return await self.provider(**kwargs).compare(self.repo, fakes.BASE_SHA, fakes.HEAD_SHA)

    async def aclose(self) -> None:
        for client in self._clients:
            await client.aclose()


@pytest.fixture(params=["github", "gitlab"])
async def host(request: pytest.FixtureRequest) -> AsyncIterator[Harness]:
    harness = Harness(request.param)
    yield harness
    await harness.aclose()


EXPECTED_FILES = [
    FileDiff(path="src/app.py", status=ChangeType.MODIFIED, additions=2, deletions=1,
             patch=fakes.PATCH_MODIFIED),
    FileDiff(path="src/new.py", status=ChangeType.ADDED, additions=2, patch=fakes.PATCH_ADDED),
    FileDiff(path="src/old.py", status=ChangeType.REMOVED, deletions=1, patch=fakes.PATCH_REMOVED),
    FileDiff(path="docs/b.md", old_path="docs/a.md", status=ChangeType.RENAMED),
    FileDiff(path="src/helpers.py", old_path="src/util.py", status=ChangeType.RENAMED,
             additions=1, deletions=1, patch=fakes.PATCH_RENAME_EDIT),
    FileDiff(path=fakes.BIG_FILE, status=ChangeType.MODIFIED, additions=1, deletions=1,
             patch=build_patch(fakes.BIG_OLD.decode(), fakes.BIG_NEW.decode())),
    FileDiff(path="assets/logo.png", status=ChangeType.MODIFIED, is_binary=True),
]  # fmt: skip


async def test_compare_normalizes_scenario(host: Harness) -> None:
    result = await host.compare()

    assert result.base_sha == fakes.BASE_SHA
    assert result.head_sha == fakes.HEAD_SHA
    assert result.truncated is False
    assert result.files == EXPECTED_FILES


async def test_compare_collects_all_commits_in_order(host: Harness) -> None:
    result = await host.compare()

    assert [c.sha for c in result.commits] == [c[0] for c in fakes.COMMITS]
    assert [c.title for c in result.commits] == [c[1] for c in fakes.COMMITS]
    assert result.commits[0].author_name == "Ada"
    assert result.commits[0].authored_at is not None
    assert result.commits[0].authored_at.tzinfo is not None


async def test_rebuilt_patch_matches_real_change(host: Harness) -> None:
    result = await host.compare()
    big = next(f for f in result.files if f.path == fakes.BIG_FILE)

    assert big.patch is not None
    assert "-INSERT INTO t VALUES (25);" in big.patch
    assert "+INSERT INTO t VALUES (2500);" in big.patch
    assert big.patch_omitted is False


async def test_fallback_budget_exhausted_marks_patch_omitted(host: Harness) -> None:
    result = await host.compare(fallback_max_files=0)
    big = next(f for f in result.files if f.path == fakes.BIG_FILE)

    assert big.patch is None
    assert big.patch_omitted is True
    assert big.is_binary is False


async def test_fallback_size_cap_marks_patch_omitted(host: Harness) -> None:
    result = await host.compare(fallback_max_bytes=100)
    big = next(f for f in result.files if f.path == fakes.BIG_FILE)

    assert big.patch is None
    assert big.patch_omitted is True


async def test_only_omitted_patches_trigger_raw_fetches(host: Harness) -> None:
    await host.compare()

    paths = host.recorder.paths()
    assert len([p for p in paths if "big.sql" in p]) == 2  # base + head of the one large file
    assert not any("logo.png" in p for p in paths)


async def test_token_is_sent_only_in_headers(host: Harness) -> None:
    await host.compare()

    assert host.recorder.requests
    for request in host.recorder.requests:
        assert fakes.TOKEN not in str(request.url)
        assert fakes.TOKEN in " ".join(request.headers.values())


async def test_unknown_ref_raises_not_found(host: Harness) -> None:
    with pytest.raises(NotFoundError) as excinfo:
        await host.provider().resolve_ref(host.repo, "does-not-exist")
    assert "does-not-exist" in str(excinfo.value)
