"""Git connections endpoints (/api/v1/git-connections)."""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AdminAuth, CurrentAuth
from app.core.crypto import encrypt_secret
from app.core.db import get_session
from app.domain.enums import GitAuthType
from app.integrations.git import (
    AccessDeniedError,
    AuthenticationError,
    GitProvider,
    GitProviderError,
    NotFoundError,
    RateLimitError,
    UnsupportedProviderError,
    build_provider,
)
from app.integrations.git.factory import DEFAULT_BASE_URLS
from app.models.git import GitConnection
from app.schemas.api import (
    CreateGitConnectionRequest,
    GitConnectionResponse,
    UpdateGitConnectionRequest,
)
from app.services.git_connections import token_health, verify_connection

router = APIRouter(prefix="/git-connections", tags=["git-connections"])

ProviderFactory = Callable[[GitConnection], GitProvider]


def get_provider_factory() -> ProviderFactory:
    """Injectable factory for tests."""
    return build_provider


def map_git_provider_error(exc: GitProviderError) -> HTTPException:
    """Convert a typed GitProviderError into a safe HTTPException."""
    if isinstance(exc, AuthenticationError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Git provider rejected the access token (invalid, expired, or revoked).",
        )
    if isinstance(exc, AccessDeniedError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Git access token lacks required permissions for this resource.",
        )
    if isinstance(exc, NotFoundError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Repository or ref not found on provider ({exc}).",
        )
    if isinstance(exc, UnsupportedProviderError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        )
    if isinstance(exc, RateLimitError):
        return HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Git provider rate limit exceeded. Try again shortly.",
        )
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"Git provider communication error ({exc}).",
    )


def _to_response(conn: GitConnection, *, now: datetime | None = None) -> GitConnectionResponse:
    return GitConnectionResponse(
        id=conn.id,
        org_id=conn.org_id,
        provider=conn.provider,
        base_url=conn.base_url,
        auth_type=conn.auth_type,
        account_login=conn.account_login,
        token_expires_at=conn.token_expires_at,
        last_verified_at=conn.last_verified_at,
        token_health=token_health(conn.token_expires_at, now=now),
        created_at=conn.created_at,
    )


async def get_org_connection(
    session: AsyncSession, connection_id: uuid.UUID, org_id: uuid.UUID
) -> GitConnection:
    conn = await session.scalar(
        select(GitConnection).where(
            GitConnection.id == connection_id, GitConnection.org_id == org_id
        )
    )
    if conn is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Git connection not found."
        )
    return conn


@router.get("", response_model=list[GitConnectionResponse])
async def list_git_connections(
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[GitConnectionResponse]:
    """List git connections in the current organization with computed token health."""
    rows = (
        await session.scalars(
            select(GitConnection)
            .where(GitConnection.org_id == ctx.org.id)
            .order_by(GitConnection.created_at.desc(), GitConnection.id.desc())
        )
    ).all()
    now = datetime.now(UTC)
    return [_to_response(row, now=now) for row in rows]


@router.post("", response_model=GitConnectionResponse, status_code=status.HTTP_201_CREATED)
async def create_git_connection(
    body: CreateGitConnectionRequest,
    ctx: AdminAuth,
    session: Annotated[AsyncSession, Depends(get_session)],
    provider_factory: Annotated[ProviderFactory, Depends(get_provider_factory)],
) -> GitConnectionResponse:
    """Create and live-verify a git connection (admin only)."""
    base_url = (body.base_url or "").strip().rstrip("/") or DEFAULT_BASE_URLS.get(body.provider, "")
    if not base_url:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"base_url is required for provider {body.provider!r}.",
        )
    conn = GitConnection(
        org_id=ctx.org.id,
        provider=body.provider,
        base_url=base_url,
        auth_type=GitAuthType.ACCESS_TOKEN,
        account_login=body.account_login.strip(),
        access_token_encrypted=encrypt_secret(body.access_token.strip()),
        token_expires_at=body.token_expires_at,
    )
    try:
        async with provider_factory(conn) as provider:
            await verify_connection(conn, provider)
    except GitProviderError as exc:
        raise map_git_provider_error(exc) from None

    session.add(conn)
    await session.commit()
    await session.refresh(conn)
    return _to_response(conn)


@router.post("/{connection_id}/verify", response_model=GitConnectionResponse)
async def verify_git_connection_endpoint(
    connection_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session)],
    provider_factory: Annotated[ProviderFactory, Depends(get_provider_factory)],
) -> GitConnectionResponse:
    """Re-verify the stored access token with the Git provider and refresh token health."""
    conn = await get_org_connection(session, connection_id, ctx.org.id)
    try:
        async with provider_factory(conn) as provider:
            await verify_connection(conn, provider)
    except GitProviderError as exc:
        raise map_git_provider_error(exc) from None

    await session.commit()
    await session.refresh(conn)
    return _to_response(conn)


@router.patch("/{connection_id}", response_model=GitConnectionResponse)
async def update_git_connection(
    connection_id: uuid.UUID,
    body: UpdateGitConnectionRequest,
    ctx: AdminAuth,
    session: Annotated[AsyncSession, Depends(get_session)],
    provider_factory: Annotated[ProviderFactory, Depends(get_provider_factory)],
) -> GitConnectionResponse:
    """Rotate a git connection's access token or update its expiry date (admin only)."""
    conn = await get_org_connection(session, connection_id, ctx.org.id)
    if "token_expires_at" in body.model_fields_set:
        conn.token_expires_at = body.token_expires_at
    if body.access_token is not None:
        conn.access_token_encrypted = encrypt_secret(body.access_token.strip())
        try:
            async with provider_factory(conn) as provider:
                await verify_connection(conn, provider)
        except GitProviderError as exc:
            raise map_git_provider_error(exc) from None

    await session.commit()
    await session.refresh(conn)
    return _to_response(conn)
