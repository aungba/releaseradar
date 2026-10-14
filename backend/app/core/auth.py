"""Authentication dependencies for FastAPI routes (M3 plan §2).

Both browser sessions (HttpOnly cookie) and scripts/CI (Authorization: Bearer) authenticate
against ``api_tokens`` rows so revoking a token immediately invalidates both.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.crypto import API_TOKEN_PREFIX, hash_api_token
from app.core.db import get_session
from app.domain.enums import UserRole
from app.models.identity import ApiToken, Organization, User

_LAST_USED_THROTTLE = timedelta(minutes=5)


@dataclass(frozen=True)
class AuthContext:
    user: User
    org: Organization
    token: ApiToken


def _extract_raw_token(request: Request, cookie_name: str) -> str | None:
    auth_header = request.headers.get("Authorization")
    if auth_header:
        scheme, _, param = auth_header.partition(" ")
        if scheme.lower() == "bearer" and param.strip():
            return param.strip()
        return None
    cookie_val = request.cookies.get(cookie_name)
    if cookie_val:
        return cookie_val.strip()
    return None


async def authenticate_token(
    session: AsyncSession, raw_token: str, *, now: datetime | None = None
) -> AuthContext:
    """Validate a plaintext ``rr_...`` token and return its user, org, and token row."""
    if not raw_token.startswith(API_TOKEN_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing access token."
        )
    now = now or datetime.now(UTC)
    token_hash = hash_api_token(raw_token)
    row = (
        await session.execute(
            select(ApiToken, User, Organization)
            .join(User, ApiToken.user_id == User.id)
            .join(Organization, User.org_id == Organization.id)
            .where(ApiToken.token_hash == token_hash)
        )
    ).first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing access token."
        )
    api_token, user, org = row
    if api_token.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Access token has been revoked."
        )
    if api_token.expires_at is not None and api_token.expires_at <= now:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Access token has expired."
        )
    if api_token.last_used_at is None or (now - api_token.last_used_at) >= _LAST_USED_THROTTLE:
        api_token.last_used_at = now
        await session.commit()
    return AuthContext(user=user, org=org, token=api_token)


async def get_auth_context(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AuthContext:
    raw_token = _extract_raw_token(request, settings.session_cookie_name)
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required."
        )
    return await authenticate_token(session, raw_token)


async def get_current_user(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> User:
    return ctx.user


async def require_admin(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> AuthContext:
    if ctx.user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required for this action."
        )
    return ctx


CurrentAuth = Annotated[AuthContext, Depends(get_auth_context)]
AdminAuth = Annotated[AuthContext, Depends(require_admin)]
