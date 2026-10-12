"""GitLab (gitlab.com and self-managed) via REST API v4."""

import asyncio
from datetime import UTC, date, datetime
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
from app.integrations.git.diffutil import count_changes, strip_file_headers
from app.integrations.git.http import HttpGitProvider

GITLAB_CLOUD_API = "https://gitlab.com/api/v4"
# Default instance limit on files returned by the compare API (``max_files`` diff limit).
COMPARE_FILE_CAP = 1000
_BINARY_MARKER = "Binary files "


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _end_of_day(value: str | None) -> datetime | None:
    """GitLab token ``expires_at`` is a date; the token is valid through that day (UTC)."""
    if not value:
        return None
    day = date.fromisoformat(value)
    return datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=UTC)


class GitLabProvider(HttpGitProvider):
    provider_name = "gitlab"

    def __init__(self, token: str, base_url: str = GITLAB_CLOUD_API, **kwargs: Any) -> None:
        super().__init__(base_url, {"PRIVATE-TOKEN": token}, **kwargs)

    @staticmethod
    def _project_path(repo: str) -> str:
        """``group/sub/project`` (or numeric id) -> ``/projects/group%2Fsub%2Fproject``."""
        if not repo.strip("/"):
            raise ValueError("GitLab project path must not be empty")
        return f"/projects/{quote(repo.strip('/'), safe='')}"

    # ------------------------------------------------------------ interface

    async def verify_credentials(self) -> Identity:
        user = await self._get_json("/user")
        expires_at: datetime | None = None
        try:
            token = await self._get_json("/personal_access_tokens/self")
            expires_at = _end_of_day(token.get("expires_at"))
        except GitProviderError:
            pass  # older instances / token types without this endpoint
        return Identity(
            username=user["username"], display_name=user.get("name"), token_expires_at=expires_at
        )

    async def get_repository(self, repo: str) -> RepositoryInfo:
        data = await self._get_json(self._project_path(repo))
        return RepositoryInfo(
            external_id=str(data["id"]),
            full_name=data["path_with_namespace"],
            default_branch=data.get("default_branch") or "main",
        )

    async def resolve_ref(self, repo: str, ref: str) -> str:
        path = f"{self._project_path(repo)}/repository/commits/{quote(ref, safe='')}"
        try:
            data = await self._get_json(path)
        except NotFoundError as exc:
            raise NotFoundError(f"gitlab: ref {ref!r} not found") from exc
        return str(data["id"])

    async def list_refs(self, repo: str, query: str = "", limit: int = 20) -> list[Ref]:
        base = f"{self._project_path(repo)}/repository"
        params = {"search": query, "per_page": limit} if query else {"per_page": limit}

        async def fetch(kind_path: str, kind: RefKind) -> list[Ref]:
            data = await self._get_json(f"{base}/{kind_path}", params=params)
            return [
                Ref(name=item["name"], kind=kind, sha=(item.get("commit") or {}).get("id"))
                for item in data
            ]

        branches, tags = await asyncio.gather(
            fetch("branches", RefKind.BRANCH), fetch("tags", RefKind.TAG)
        )
        return (branches + tags)[:limit]

    async def compare(self, repo: str, base_sha: str, head_sha: str) -> CompareResult:
        data = await self._get_json(
            f"{self._project_path(repo)}/repository/compare",
            # straight=false -> merge-base comparison, same semantics as GitHub's base...head
            params={"from": base_sha, "to": head_sha, "straight": "false"},
        )
        commits = [self._commit(c) for c in data.get("commits") or []]
        raw_diffs: list[dict[str, Any]] = data.get("diffs") or []

        budget = self._new_budget()
        files: list[FileDiff] = []
        for raw in raw_diffs:
            file = self._file(raw)
            if self._needs_rebuild(file, raw):
                file = await self._rebuild_patch(repo, file, base_sha, head_sha, budget)
            files.append(file)

        truncated = bool(data.get("compare_timeout")) or len(raw_diffs) >= COMPARE_FILE_CAP
        return CompareResult(
            base_sha=base_sha, head_sha=head_sha, commits=commits, files=files, truncated=truncated
        )

    # ------------------------------------------------------------ mapping

    @staticmethod
    def _needs_rebuild(file: FileDiff, raw: dict[str, Any]) -> bool:
        """GitLab returns an empty ``diff`` for collapsed / too-large files."""
        return (
            file.patch is None
            and not file.is_binary
            and bool(raw.get("too_large") or raw.get("collapsed"))
        )

    @staticmethod
    def _commit(data: dict[str, Any]) -> Commit:
        return Commit(
            sha=data["id"],
            title=data.get("title") or "",
            author_name=data.get("author_name"),
            authored_at=_parse_dt(data.get("authored_date")),
        )

    @staticmethod
    def _file(data: dict[str, Any]) -> FileDiff:
        if data.get("new_file"):
            status = ChangeType.ADDED
        elif data.get("deleted_file"):
            status = ChangeType.REMOVED
        elif data.get("renamed_file"):
            status = ChangeType.RENAMED
        else:
            status = ChangeType.MODIFIED

        diff: str = data.get("diff") or ""
        is_binary = _BINARY_MARKER in diff and "\n@@" not in diff and not diff.startswith("@@")
        patch = strip_file_headers(diff) if diff and not is_binary else None
        additions, deletions = count_changes(patch)
        return FileDiff(
            path=data["new_path"],
            old_path=data.get("old_path") if status is ChangeType.RENAMED else None,
            status=status,
            additions=additions,
            deletions=deletions,
            patch=patch,
            is_binary=is_binary,
        )

    async def _fetch_raw(self, repo: str, path: str, sha: str) -> bytes | None:
        return await self._read_capped(
            f"{self._project_path(repo)}/repository/files/{quote(path, safe='')}/raw",
            params={"ref": sha},
        )
