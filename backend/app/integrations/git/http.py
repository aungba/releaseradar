"""HTTP plumbing shared by REST-based providers: error mapping, pagination, patch fallback."""

import re
from collections.abc import AsyncIterator, Mapping
from types import TracebackType
from typing import Any, NoReturn, Self

import httpx

from app.domain.enums import ChangeType
from app.integrations.git.base import (
    AccessDeniedError,
    AuthenticationError,
    FileDiff,
    GitProviderError,
    NotFoundError,
    RateLimitError,
)
from app.integrations.git.diffutil import build_patch, count_changes, decode_text

_LINK_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
DEFAULT_MAX_PAGES = 50


class FallbackBudget:
    """Limits how many large files we re-fetch to rebuild an omitted patch."""

    def __init__(self, max_files: int, max_bytes_per_side: int) -> None:
        self.remaining_files = max_files
        self.max_bytes_per_side = max_bytes_per_side

    def take(self) -> bool:
        if self.remaining_files <= 0:
            return False
        self.remaining_files -= 1
        return True


class HttpGitProvider:
    """Base class: owns the ``httpx.AsyncClient`` and maps HTTP failures to typed errors.

    Tokens are only ever placed in headers; error messages never include them.
    """

    provider_name = "git"

    def __init__(
        self,
        base_url: str,
        headers: Mapping[str, str],
        *,
        client: httpx.AsyncClient | None = None,
        max_pages: int = DEFAULT_MAX_PAGES,
        fallback_max_files: int = 25,
        fallback_max_bytes: int = 256 * 1024,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
        self._base_url = base_url.rstrip("/")
        self._headers = dict(headers)
        self._max_pages = max_pages
        self._fallback_max_files = fallback_max_files
        self._fallback_max_bytes = fallback_max_bytes

    # ------------------------------------------------------------ lifecycle

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc: BaseException | None = None,
        tb: TracebackType | None = None,
    ) -> None:
        await self.aclose()

    # ------------------------------------------------------------ requests

    def _url(self, path: str) -> str:
        return path if path.startswith("http") else f"{self._base_url}{path}"

    async def _request(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        try:
            response = await self._client.get(
                self._url(path), params=params, headers={**self._headers, **(headers or {})}
            )
        except httpx.TimeoutException as exc:
            raise GitProviderError(f"{self.provider_name}: request timed out") from exc
        except httpx.HTTPError as exc:
            raise GitProviderError(f"{self.provider_name}: network error ({exc})") from exc
        if response.is_error:
            self._raise_for_status(response)
        return response

    async def _get_json(self, path: str, **kwargs: Any) -> Any:
        return (await self._request(path, **kwargs)).json()

    async def _paginate(
        self, path: str, params: Mapping[str, Any] | None = None
    ) -> AsyncIterator[httpx.Response]:
        """Yield each page, following RFC 5988 ``Link: rel="next"`` (GitHub and GitLab)."""
        url: str | None = path
        current_params: Mapping[str, Any] | None = params
        for _ in range(self._max_pages):
            if url is None:
                return
            response = await self._request(url, params=current_params)
            yield response
            match = _LINK_NEXT.search(response.headers.get("link", ""))
            url = match.group(1) if match else None
            current_params = None  # the next link already carries the query string

    def _raise_for_status(self, response: httpx.Response) -> NoReturn:
        raise self._error_for(response)

    def _error_for(self, response: httpx.Response) -> GitProviderError:
        status = response.status_code
        name = self.provider_name
        error: GitProviderError
        if status == 401:
            error = AuthenticationError(f"{name}: token is invalid, expired or revoked")
        elif status == 429 or (status == 403 and self._is_rate_limited(response)):
            retry_after = response.headers.get("retry-after")
            error = RateLimitError(
                f"{name}: rate limit exceeded",
                retry_after_seconds=int(retry_after)
                if retry_after and retry_after.isdigit()
                else None,
            )
        elif status == 403:
            error = AccessDeniedError(f"{name}: token lacks access to this resource")
        elif status == 404:
            error = NotFoundError(f"{name}: not found")
        else:
            error = GitProviderError(f"{name}: unexpected HTTP {status}")
        error.status_code = status
        return error

    @staticmethod
    def _is_rate_limited(response: httpx.Response) -> bool:
        return response.headers.get("x-ratelimit-remaining") == "0" or (
            "retry-after" in response.headers
        )

    # ------------------------------------------------------------ patch fallback

    def _new_budget(self) -> FallbackBudget:
        return FallbackBudget(self._fallback_max_files, self._fallback_max_bytes)

    async def _fetch_raw(self, repo: str, path: str, sha: str) -> bytes | None:
        """Raw file bytes at ``sha``; implemented per provider."""
        raise NotImplementedError

    async def _rebuild_patch(
        self,
        repo: str,
        file: FileDiff,
        base_sha: str,
        head_sha: str,
        budget: FallbackBudget,
    ) -> FileDiff:
        """Recreate a patch the provider omitted (too large/collapsed) from raw file contents."""
        if not budget.take():
            return file.model_copy(update={"patch_omitted": True})

        old: bytes | None = b""
        new: bytes | None = b""
        try:
            if file.status is not ChangeType.ADDED:
                old = await self._fetch_raw(repo, file.old_path or file.path, base_sha)
            if file.status is not ChangeType.REMOVED:
                new = await self._fetch_raw(repo, file.path, head_sha)
        except NotFoundError:
            return file.model_copy(update={"patch_omitted": True})

        if old is None or new is None:  # exceeded size cap
            return file.model_copy(update={"patch_omitted": True})
        old_text, new_text = decode_text(old), decode_text(new)
        if old_text is None or new_text is None:
            return file.model_copy(update={"is_binary": True, "patch_omitted": False})

        patch = build_patch(old_text, new_text)
        additions, deletions = count_changes(patch)
        return file.model_copy(
            update={
                "patch": patch,
                "additions": additions,
                "deletions": deletions,
                "patch_omitted": False,
            }
        )

    async def _read_capped(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> bytes | None:
        """Stream raw bytes; return None as soon as the per-side fallback cap is exceeded."""
        request_headers = {**self._headers, **(headers or {})}
        try:
            async with self._client.stream(
                "GET", self._url(path), params=params, headers=request_headers
            ) as response:
                if response.is_error:
                    self._raise_for_status(response)
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self._fallback_max_bytes:
                        return None
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.TimeoutException as exc:
            raise GitProviderError(f"{self.provider_name}: request timed out") from exc
        except httpx.HTTPError as exc:
            raise GitProviderError(f"{self.provider_name}: network error ({exc})") from exc
