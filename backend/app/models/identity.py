import uuid
from datetime import datetime, time

from sqlalchemy import CHAR, ForeignKey, String, Time, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import UserRole
from app.models.base import Base, created_at_column, enum_check, tz_datetime, uuid_pk


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(100))
    timezone: Mapped[str] = mapped_column(String(64), server_default=text("'UTC'"))
    digest_local_time: Mapped[time] = mapped_column(Time, server_default=text("'18:00:00'"))
    created_at: Mapped[datetime] = created_at_column()


class User(Base):
    __tablename__ = "users"
    __table_args__ = (enum_check("role", UserRole),)

    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    email: Mapped[str] = mapped_column(String(255), unique=True)  # MVP: one org per user
    display_name: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(20), server_default=text(f"'{UserRole.MEMBER}'"))
    created_at: Mapped[datetime] = created_at_column()


class ApiToken(Base):
    """ReleaseRadar-issued access token. Only the SHA-256 hash is stored."""

    __tablename__ = "api_tokens"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    token_prefix: Mapped[str] = mapped_column(String(16))
    token_hash: Mapped[str] = mapped_column(CHAR(64), unique=True)
    expires_at: Mapped[datetime | None] = tz_datetime()
    last_used_at: Mapped[datetime | None] = tz_datetime()
    revoked_at: Mapped[datetime | None] = tz_datetime()
    created_at: Mapped[datetime] = created_at_column()
