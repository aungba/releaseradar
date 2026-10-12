import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import (
    AuditEventType,
    ChangeType,
    DigestStatus,
    HazardCategory,
    JobStatus,
    PrerequisiteKind,
    ReviewType,
    Severity,
    Verdict,
)
from app.models.base import Base, created_at_column, enum_check, tz_datetime, uuid_pk

_EMPTY_JSONB = text("'{}'::jsonb")


class DailyDigest(Base):
    """Org-wide end-of-day rollup; per-project results are review_jobs with digest_id set."""

    __tablename__ = "daily_digests"
    __table_args__ = (
        enum_check("status", DigestStatus),
        CheckConstraint("org_health_score BETWEEN 0 AND 100", name="org_health_score_range"),
        CheckConstraint("window_end > window_start", name="window_order"),
        UniqueConstraint("org_id", "digest_date"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    digest_date: Mapped[date] = mapped_column(Date)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), server_default=text(f"'{DigestStatus.QUEUED}'"))
    executive_summary: Mapped[str | None] = mapped_column(Text)
    org_health_score: Mapped[int | None] = mapped_column(Integer)
    totals: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=_EMPTY_JSONB)
    created_at: Mapped[datetime] = created_at_column()
    completed_at: Mapped[datetime | None] = tz_datetime()


class ReviewJob(Base):
    """One analysis run. Result columns stay NULL until status = COMPLETED."""

    __tablename__ = "review_jobs"
    __table_args__ = (
        enum_check("review_type", ReviewType),
        enum_check("status", JobStatus),
        enum_check("verdict", Verdict),
        CheckConstraint("risk_score BETWEEN 0 AND 100", name="risk_score_range"),
        CheckConstraint(
            f"status <> '{JobStatus.COMPLETED}' OR ("
            "verdict IS NOT NULL AND risk_score IS NOT NULL AND executive_summary IS NOT NULL"
            " AND rollback_safe IS NOT NULL AND completed_at IS NOT NULL)",
            name="completed_requires_result",
        ),
        CheckConstraint(
            f"status <> '{JobStatus.FAILED}' OR error_message IS NOT NULL",
            name="failed_requires_error",
        ),
        CheckConstraint(
            f"review_type <> '{ReviewType.DAILY_DIGEST}' OR digest_id IS NOT NULL",
            name="digest_type_requires_digest",
        ),
        CheckConstraint(
            f"review_type <> '{ReviewType.PULL_REQUEST}' OR pr_number IS NOT NULL",
            name="pr_type_requires_pr_number",
        ),
        Index("ix_review_jobs_project_id_created_at", "project_id", text("created_at DESC")),
        Index(
            "ix_review_jobs_digest_id", "digest_id", postgresql_where=text("digest_id IS NOT NULL")
        ),
        Index(
            "ix_review_jobs_active",
            "status",
            postgresql_where=text(f"status IN ('{JobStatus.QUEUED}', '{JobStatus.RUNNING}')"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE")
    )
    digest_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("daily_digests.id", ondelete="CASCADE")
    )
    triggered_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    review_type: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20), server_default=text(f"'{JobStatus.QUEUED}'"))
    base_ref: Mapped[str] = mapped_column(String(255))
    target_ref: Mapped[str] = mapped_column(String(255))
    base_sha: Mapped[str | None] = mapped_column(String(64))
    target_sha: Mapped[str | None] = mapped_column(String(64))
    pr_number: Mapped[int | None] = mapped_column(Integer)
    verdict: Mapped[str | None] = mapped_column(String(30))
    risk_score: Mapped[int | None] = mapped_column(Integer)
    executive_summary: Mapped[str | None] = mapped_column(Text)
    rollback_safe: Mapped[bool | None] = mapped_column(Boolean)
    rollback_notes: Mapped[str | None] = mapped_column(Text)
    raw_metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=_EMPTY_JSONB)
    llm_model: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()
    started_at: Mapped[datetime | None] = tz_datetime()
    completed_at: Mapped[datetime | None] = tz_datetime()


class ReviewHazard(Base):
    """A flagged hazard. Breaking Changes Matrix = rows WHERE is_breaking."""

    __tablename__ = "review_hazards"
    __table_args__ = (
        enum_check("category", HazardCategory),
        enum_check("severity", Severity),
        enum_check("change_type", ChangeType),
        CheckConstraint(
            "NOT is_breaking OR (change_type IS NOT NULL AND affected_symbol IS NOT NULL)",
            name="breaking_requires_symbol",
        ),
        Index("ix_review_hazards_review_job_id_severity", "review_job_id", "severity"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    review_job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("review_jobs.id", ondelete="CASCADE")
    )
    category: Mapped[str] = mapped_column(String(30))
    severity: Mapped[str] = mapped_column(String(20))
    file_path: Mapped[str] = mapped_column(Text)
    hazard_title: Mapped[str] = mapped_column(String(255))
    explanation: Mapped[str] = mapped_column(Text)
    remediation: Mapped[str] = mapped_column(Text)
    is_breaking: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    change_type: Mapped[str | None] = mapped_column(String(20))
    affected_symbol: Mapped[str | None] = mapped_column(String(255))
    acknowledged_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    acknowledged_at: Mapped[datetime | None] = tz_datetime()


class ReviewPrerequisite(Base):
    """Deployment checklist item (required env var or pre-deploy action)."""

    __tablename__ = "review_prerequisites"
    __table_args__ = (
        enum_check("kind", PrerequisiteKind),
        Index("ix_review_prerequisites_review_job_id_sort_order", "review_job_id", "sort_order"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    review_job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("review_jobs.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(30))
    description: Mapped[str] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    completed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    completed_at: Mapped[datetime | None] = tz_datetime()


class ReviewAuditEvent(Base):
    """Append-only sign-off trail. actor_display is a snapshot that survives user deletion."""

    __tablename__ = "review_audit_events"
    __table_args__ = (
        enum_check("event_type", AuditEventType),
        Index("ix_review_audit_events_review_job_id_created_at", "review_job_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    review_job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("review_jobs.id", ondelete="CASCADE")
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    actor_display: Mapped[str] = mapped_column(String(255))
    event_type: Mapped[str] = mapped_column(String(40))
    hazard_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("review_hazards.id", ondelete="CASCADE")
    )
    prerequisite_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("review_prerequisites.id", ondelete="CASCADE")
    )
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()
