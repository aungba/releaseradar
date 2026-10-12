"""GitHub (github.com and GitHub Enterprise Server) via REST API v3."""

import asyncio
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from app.domain.enums import ChangeType
from app.integrations.git.base import (
    Commit,
    CompareResult,
    FileDiff,
    GitProviderError,
    Identity,
    NotFoundError,
    Ref,
    RefKind,
    RepositoryInfo,
)
from app.integrations.git.diffutil import count_changes
from app.integrations.git.http import HttpGitProvider

GITHUB_CLOUD_API = "https://api.github.com"
API_VERSION = "2022-11-28"
# GitHub's compare endpoint returns at most 300 files for the whole comparison.
COMPARE_FILE_CAP = 300

_STATUS = {
    "added": ChangeType.ADDED,
    "copied": ChangeType.ADDED,
    "removed": ChangeType.REMOVED,
    "renamed": ChangeType.RENAMED,
    "modified": ChangeType.MODIFIED,
    "changed": ChangeType.MODIFIED,
    "unchanged": ChangeType.MODIFIED,
}


def parse_token_expiration(value: str | None) -> datetime | None:
    """Parse ``github-authentication-token-expiration`` (e.g. ``2026-11-11 08:00:00 UTC``)."""
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S %z"):
        try:
            parsed = datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


class GitHubProvider(HttpGitProvider):
    provider_name = "github"

    def __init__(self, token: str, base_url: str = GITHUB_CLOUD_API, **kwargs: Any) -> None:
        super().__init__(
            base_url,
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
            },
            **kwargs,
        )

    @staticmethod
    def _repo_path(repo: str) -> str:
        owner, _, name = repo.partition("/")
        if not owner or not name or "/" in name:
            raise ValueError(f"GitHub repository must be 'owner/name', got {repo!r}")
        return f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"

    # ------------------------------------------------------------ interface

    async def verify_credentials(self) -> Identity:
        response = await self._request("/user")
        data = response.json()
        return Identity(
            username=data["login"],
            display_name=data.get("name"),
            token_expires_at=parse_token_expiration(
                response.headers.get("github-authentication-token-expiration")
            ),
        )

    async def get_repository(self, repo: str) -> RepositoryInfo:
        data = await self._get_json(self._repo_path(repo))
        return RepositoryInfo(
            external_id=str(data["id"]),
            full_name=data["full_name"],
            default_branch=data["default_branch"],
        )

    async def resolve_ref(self, repo: str, ref: str) -> str:
        path = f"{self._repo_path(repo)}/commits/{quote(ref, safe='')}"
        try:
            response = await self._request(path, headers={"Accept": "application/vnd.github.sha"})
        except GitProviderError as exc:
            # 422 = "No commit found for SHA"
            if isinstance(exc, NotFoundError) or exc.status_code == 422:
                raise NotFoundError(f"github: ref {ref!r} not found") from exc
            raise
        return response.text.strip()

    async def list_refs(self, repo: str, query: str = "", limit: int = 20) -> list[Ref]:
        base = self._repo_path(repo)
        prefix = quote(query, safe="/")

        async def matching(namespace: str, kind: RefKind) -> list[Ref]:
            data = await self._get_json(f"{base}/git/matching-refs/{namespace}/{prefix}")
            return [
                Ref(
                    name=item["ref"].removeprefix(f"refs/{namespace}/"),
                    kind=kind,
                    # Annotated tags point at a tag object, not a commit.
                    sha=item["object"]["sha"] if item["object"]["type"] == "commit" else None,
                )
                for item in data
            ]

        branches, tags = await asyncio.gather(
            matching("heads", RefKind.BRANCH), matching("tags", RefKind.TAG)
        )
        return (branches + tags)[:limit]

    async def compare(self, repo: str, base_sha: str, head_sha: str) -> CompareResult:
        path = (
            f"{self._repo_path(repo)}/compare/"
            f"{quote(base_sha, safe='')}...{quote(head_sha, safe='')}"
        )
        commits: list[Commit] = []
        raw_files: list[dict[str, Any]] = []
        total_commits = 0
        async for page in self._paginate(path, params={"per_page": 100}):
            data = page.json()
            total_commits = data.get("total_commits", total_commits)
            commits.extend(self._commit(c) for c in data.get("commits", []))
            if not raw_files:  # files are only returned on the first page
                raw_files = data.get("files", [])

        budget = self._new_budget()
        files: list[FileDiff] = []
        for raw in raw_files:
            file = self._file(raw)
            if self._needs_rebuild(file, raw):
                file = await self._rebuild_patch(repo, file, base_sha, head_sha, budget)
            files.append(file)

        truncated = len(raw_files) >= COMPARE_FILE_CAP or len(commits) < total_commits
        return CompareResult(
            base_sha=base_sha, head_sha=head_sha, commits=commits, files=files, truncated=truncated
        )

    # ------------------------------------------------------------ mapping

    @staticmethod
    def _needs_rebuild(file: FileDiff, raw: dict[str, Any]) -> bool:
        """GitHub omits ``patch`` for large diffs while still reporting line counts."""
        return file.patch is None and not file.is_binary and int(raw.get("changes", 0)) > 0

    @staticmethod
    def _commit(data: dict[str, Any]) -> Commit:
        commit = data.get("commit", {})
        author = commit.get("author") or {}
        message: str = commit.get("message") or ""
        return Commit(
            sha=data["sha"],
            title=message.splitlines()[0] if message else "",
            author_name=author.get("name"),
            authored_at=_parse_dt(author.get("date")),
        )

    @staticmethod
    def _file(data: dict[str, Any]) -> FileDiff:
        patch = data.get("patch")
        status = _STATUS.get(data.get("status", "modified"), ChangeType.MODIFIED)
        changes = int(data.get("changes", 0))
        additions = int(data.get("additions", 0))
        deletions = int(data.get("deletions", 0))
        if patch is not None and not (additions or deletions):
            additions, deletions = count_changes(patch)
        # Binary files: no patch and no line changes. Pure renames look the same but aren't binary.
        is_binary = patch is None and changes == 0 and status is not ChangeType.RENAMED
        return FileDiff(
            path=data["filename"],
            old_path=data.get("previous_filename") if status is ChangeType.RENAMED else None,
            status=status,
            additions=additions,
            deletions=deletions,
            patch=patch,
            is_binary=is_binary,
        )

    async def _fetch_raw(self, repo: str, path: str, sha: str) -> bytes | None:
        return await self._read_capped(
            f"{self._repo_path(repo)}/contents/{quote(path)}",
            params={"ref": sha},
            headers={"Accept": "application/vnd.github.raw+json"},
        )
