"""Provider-agnostic Git interface.

The analysis engine only ever sees the normalized models in this module
(``CompareResult`` / ``FileDiff``); it never knows which provider a diff came from.
"""

from datetime import datetime
from enum import StrEnum
from types import TracebackType
from typing import Protocol, Self

from pydantic import BaseModel, ConfigDict

from app.domain.enums import ChangeType

# ---------------------------------------------------------------- models


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class Identity(_Frozen):
    """Who the token authenticates as, and when the token expires (if the provider says)."""

    username: str
    display_name: str | None = None
    token_expires_at: datetime | None = None


class RepositoryInfo(_Frozen):
    external_id: str
    full_name: str
    default_branch: str


class RefKind(StrEnum):
    BRANCH = "branch"
    TAG = "tag"


class Ref(_Frozen):
    name: str
    kind: RefKind
    sha: str | None = None  # None for annotated tags (resolve_ref gives the commit)


class Commit(_Frozen):
    sha: str
    title: str
    author_name: str | None = None
    authored_at: datetime | None = None


class FileDiff(_Frozen):
    path: str
    old_path: str | None = None  # set only for renames
    status: ChangeType
    additions: int = 0
    deletions: int = 0
    patch: str | None = None  # unified hunks ("@@ ..."), no file headers
    is_binary: bool = False
    # True when the change has text content we could not obtain (too large, fetch cap hit).
    patch_omitted: bool = False


class CompareResult(_Frozen):
    base_sha: str
    head_sha: str
    commits: list[Commit]
    files: list[FileDiff]
    # True when the provider did not return the complete change set (file caps, timeouts).
    truncated: bool = False


# ---------------------------------------------------------------- errors


class GitProviderError(Exception):
    """Base class for provider failures. ``status_code`` is set for HTTP errors."""

    status_code: int | None = None


class AuthenticationError(GitProviderError):
    """Token missing, invalid, expired or revoked (HTTP 401)."""


class AccessDeniedError(GitProviderError):
    """Token is valid but lacks access/scope for this resource (HTTP 403)."""


class NotFoundError(GitProviderError):
    """Repository or ref does not exist, or is invisible to this token (HTTP 404)."""


class RateLimitError(GitProviderError):
    def __init__(self, message: str, retry_after_seconds: int | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class UnsupportedProviderError(GitProviderError):
    """Provider not implemented yet (e.g. Bitbucket in Phase 1)."""


# ---------------------------------------------------------------- interface


class GitProvider(Protocol):
    """Read-only operations ReleaseRadar needs from any Git host.

    ``repo`` is the provider's repository identifier as stored in ``projects.repo_identifier``
    (``owner/repo`` on GitHub, ``group/sub/project`` on GitLab).
    """

    async def verify_credentials(self) -> Identity: ...

    async def get_repository(self, repo: str) -> RepositoryInfo: ...

    async def resolve_ref(self, repo: str, ref: str) -> str:
        """Branch, tag or SHA -> full commit SHA. Raises ``NotFoundError``."""
        ...

    async def list_refs(self, repo: str, query: str = "", limit: int = 20) -> list[Ref]:
        """Branches and tags whose name starts with/contains ``query`` (autocomplete)."""
        ...

    async def compare(self, repo: str, base_sha: str, head_sha: str) -> CompareResult:
        """Changes reachable from ``head_sha`` but not ``base_sha`` (merge-base semantics)."""
        ...

    async def aclose(self) -> None: ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc: BaseException | None = None,
        tb: TracebackType | None = None,
    ) -> None: ...
