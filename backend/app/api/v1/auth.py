"""Authentication & self-service access token endpoints (/api/v1/auth, /api/v1/me)."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthContext, CurrentAuth, authenticate_token
from app.core.config import Settings, get_settings
from app.core.crypto import generate_api_token
from app.core.db import get_session
from app.models.identity import ApiToken
from app.schemas.api import (
    ApiTokenResponse,
    CreateApiTokenRequest,
    CreatedApiTokenResponse,
    CurrentUserResponse,
    OrganizationResponse,
    SessionLoginRequest,
    UserResponse,
)

router = APIRouter(tags=["auth"])


def _to_current_user_response(ctx: AuthContext) -> CurrentUserResponse:
    return CurrentUserResponse(
        user=UserResponse.model_validate(ctx.user),
        organization=OrganizationResponse.model_validate(ctx.org),
        current_token_id=ctx.token.id,
        current_token_expires_at=ctx.token.expires_at,
    )


@router.post("/auth/session", response_model=CurrentUserResponse)
async def create_session(
    body: SessionLoginRequest,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CurrentUserResponse:
    """Exchange a ReleaseRadar access token for an HttpOnly session cookie."""
    ctx = await authenticate_token(session, body.token.strip())
    max_age: int | None = None
    if ctx.token.expires_at is not None:
        remaining = int((ctx.token.expires_at - datetime.now(UTC)).total_seconds())
        max_age = max(0, remaining)
    response.set_cookie(
        key=settings.session_cookie_name,
        value=body.token.strip(),
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        max_age=max_age,
        path="/",
    )
    return _to_current_user_response(ctx)


@router.delete("/auth/session", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """Clear the HttpOnly session cookie."""
    response.delete_cookie(key=settings.session_cookie_name, path="/")


@router.get("/me", response_model=CurrentUserResponse)
async def get_me(ctx: CurrentAuth) -> CurrentUserResponse:
    """Return the authenticated user, organization, and active token metadata."""
    return _to_current_user_response(ctx)


@router.get("/me/tokens", response_model=list[ApiTokenResponse])
async def list_my_tokens(
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[ApiTokenResponse]:
    """List access tokens belonging to the current user (newest first)."""
    rows = (
        await session.scalars(
            select(ApiToken)
            .where(ApiToken.user_id == ctx.user.id)
            .order_by(ApiToken.created_at.desc(), ApiToken.id.desc())
        )
    ).all()
    return [ApiTokenResponse.model_validate(row) for row in rows]


@router.post(
    "/me/tokens",
    response_model=CreatedApiTokenResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_my_token(
    body: CreateApiTokenRequest,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CreatedApiTokenResponse:
    """Issue a new access token for the current user. Plaintext is returned once."""
    now = datetime.now(UTC)
    expires_at = (
        now + timedelta(days=body.expires_in_days) if body.expires_in_days is not None else None
    )
    issued = generate_api_token()
    row = ApiToken(
        user_id=ctx.user.id,
        name=body.name.strip(),
        token_prefix=issued.display_prefix,
        token_hash=issued.token_hash,
        expires_at=expires_at,
        created_at=now,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return CreatedApiTokenResponse(
        id=row.id,
        name=row.name,
        token_prefix=row.token_prefix,
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
        created_at=row.created_at,
        plaintext=issued.plaintext,
    )


@router.delete("/me/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_my_token(
    token_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """Revoke one of the current user's access tokens."""
    row = await session.scalar(
        select(ApiToken).where(ApiToken.id == token_id, ApiToken.user_id == ctx.user.id)
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found.")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        await session.commit()
