"""Domain enums shared by the database (CHECK constraints) and the LLM contract.

Keeping one definition guarantees the DB and Pydantic schemas never drift (PRD §5 / §6).
"""

from enum import StrEnum


class UserRole(StrEnum):
    ADMIN = "ADMIN"
    MEMBER = "MEMBER"


class GitProviderKind(StrEnum):
    GITHUB = "github"
    GITLAB = "gitlab"
    BITBUCKET = "bitbucket"


class GitAuthType(StrEnum):
    ACCESS_TOKEN = "ACCESS_TOKEN"
    APP_INSTALLATION = "APP_INSTALLATION"


class ReviewType(StrEnum):
    DAILY_DIGEST = "DAILY_DIGEST"
    PULL_REQUEST = "PULL_REQUEST"
    ON_DEMAND_COMPARE = "ON_DEMAND_COMPARE"


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class DigestStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class Verdict(StrEnum):
    APPROVED = "APPROVED"
    APPROVED_WITH_CAUTION = "APPROVED_WITH_CAUTION"
    BLOCKED = "BLOCKED"


class Severity(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class HazardCategory(StrEnum):
    API_CONTRACT = "API_CONTRACT"
    DATABASE = "DATABASE"
    RUNTIME_ENV = "RUNTIME_ENV"
    SECURITY = "SECURITY"
    SHARED_STATE = "SHARED_STATE"


class ChangeType(StrEnum):
    ADDED = "ADDED"
    MODIFIED = "MODIFIED"
    REMOVED = "REMOVED"
    RENAMED = "RENAMED"


class PrerequisiteKind(StrEnum):
    REQUIRED_ENV_VAR = "REQUIRED_ENV_VAR"
    PRE_DEPLOY_ACTION = "PRE_DEPLOY_ACTION"


class AuditEventType(StrEnum):
    HAZARD_ACKNOWLEDGED = "HAZARD_ACKNOWLEDGED"
    HAZARD_UNACKNOWLEDGED = "HAZARD_UNACKNOWLEDGED"
    PREREQUISITE_COMPLETED = "PREREQUISITE_COMPLETED"
    PREREQUISITE_REOPENED = "PREREQUISITE_REOPENED"
    RELEASE_SIGNED_OFF = "RELEASE_SIGNED_OFF"
    SIGNOFF_REVOKED = "SIGNOFF_REVOKED"


class RiskRating(StrEnum):
    GREEN = "GREEN"
    AMBER = "AMBER"
    RED = "RED"


_VERDICT_TO_RATING = {
    Verdict.APPROVED: RiskRating.GREEN,
    Verdict.APPROVED_WITH_CAUTION: RiskRating.AMBER,
    Verdict.BLOCKED: RiskRating.RED,
}


def risk_rating_for(verdict: Verdict) -> RiskRating:
    """Display mapping used by the Daily Radar and banners (PRD §5)."""
    return _VERDICT_TO_RATING[verdict]
