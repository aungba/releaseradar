"""Import every model so ``Base.metadata`` is complete (Alembic autogenerate relies on this)."""

from app.models.base import Base
from app.models.git import GitConnection, Project
from app.models.identity import ApiToken, Organization, User
from app.models.review import (
    DailyDigest,
    ReviewAuditEvent,
    ReviewHazard,
    ReviewJob,
    ReviewPrerequisite,
)

__all__ = [
    "ApiToken",
    "Base",
    "DailyDigest",
    "GitConnection",
    "Organization",
    "Project",
    "ReviewAuditEvent",
    "ReviewHazard",
    "ReviewJob",
    "ReviewPrerequisite",
    "User",
]
