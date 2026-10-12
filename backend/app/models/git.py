import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import GitAuthType, GitProviderKind
from app.models.base import Base, created_at_column, enum_check, tz_datetime, uuid_pk


class GitConnection(Base):
    """Credentials for one provider account/group/workspace (GitHub, GitLab, Bitbucket)."""

    __tablename__ = "git_connections"
    __table_args__ = (
        enum_check("provider", GitProviderKind),
        enum_check("auth_type", GitAuthType),
        CheckConstraint(
            f"(auth_type = '{GitAuthType.ACCESS_TOKEN}' AND access_token_encrypted IS NOT NULL)"
            f" OR (auth_type = '{GitAuthType.APP_INSTALLATION}'"
            " AND external_installation_id IS NOT NULL)",
            name="credentials_match_auth_type",
        ),
        UniqueConstraint("id", "org_id"),  # target of the composite FK on projects
        UniqueConstraint("provider", "external_installation_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(20))
    base_url: Mapped[str] = mapped_column(String(255))
    auth_type: Mapped[str] = mapped_column(
        String(20), server_default=text(f"'{GitAuthType.ACCESS_TOKEN}'")
    )
    external_installation_id: Mapped[str | None] = mapped_column(String(100))
    account_login: Mapped[str] = mapped_column(String(255))
    auth_username: Mapped[str | None] = mapped_column(String(255))
    access_token_encrypted: Mapped[str | None] = mapped_column(Text)
    token_expires_at: Mapped[datetime | None] = tz_datetime()
    last_verified_at: Mapped[datetime | None] = tz_datetime()
    webhook_secret_encrypted: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()


class Project(Base):
    """A monitored repository."""

    __tablename__ = "projects"
    __table_args__ = (
        # The connection must belong to the same org as the project.
        ForeignKeyConstraint(
            ["git_connection_id", "org_id"],
            ["git_connections.id", "git_connections.org_id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("git_connection_id", "repo_identifier"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    git_connection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    name: Mapped[str] = mapped_column(String(100))
    repo_identifier: Mapped[str] = mapped_column(String(255))
    external_repo_id: Mapped[str | None] = mapped_column(String(100))
    default_branch: Mapped[str] = mapped_column(String(100), server_default=text("'main'"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = created_at_column()
