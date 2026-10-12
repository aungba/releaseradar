"""Git connection health: verify a stored access token and warn before it expires."""

from datetime import UTC, datetime, timedelta
from enum import StrEnum

from app.integrations.git.base import GitProvider, Identity
from app.models.git import GitConnection

EXPIRY_WARNING_DAYS = 14


class TokenHealth(StrEnum):
    OK = "OK"
    EXPIRING_SOON = "EXPIRING_SOON"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"  # provider did not report an expiry and none was entered


def token_health(
    expires_at: datetime | None,
    *,
    now: datetime | None = None,
    warn_days: int = EXPIRY_WARNING_DAYS,
) -> TokenHealth:
    if expires_at is None:
        return TokenHealth.UNKNOWN
    now = now or datetime.now(UTC)
    if expires_at <= now:
        return TokenHealth.EXPIRED
    if expires_at - now <= timedelta(days=warn_days):
        return TokenHealth.EXPIRING_SOON
    return TokenHealth.OK


async def verify_connection(
    connection: GitConnection, provider: GitProvider, *, now: datetime | None = None
) -> Identity:
    """Call the provider with the stored token and record the result on ``connection``.

    Raises the provider's typed errors (e.g. ``AuthenticationError``) unchanged; the caller
    owns the session and commits. A provider-reported expiry overrides a manually entered one,
    but a missing one never erases it.
    """
    identity = await provider.verify_credentials()
    connection.last_verified_at = now or datetime.now(UTC)
    if identity.token_expires_at is not None:
        connection.token_expires_at = identity.token_expires_at
    return identity
