"""Interactive checklist, release sign-off, and append-only audit trail service (M3 plan §4)."""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import AuditEventType, JobStatus, Severity, Verdict
from app.models.identity import User
from app.models.review import ReviewAuditEvent, ReviewHazard, ReviewJob, ReviewPrerequisite
from app.schemas.api import SignoffSummary

BLOCKING_SEVERITIES = frozenset({Severity.CRITICAL, Severity.HIGH})
MIN_BLOCKED_OVERRIDE_NOTE_LEN = 10


class SignoffRuleError(Exception):
    """Raised when a sign-off or checklist action violates business rules (maps to 409 Conflict)."""


def _actor_display(user: User) -> str:
    return f"{user.display_name} <{user.email}>"


def compute_signoff_summary(
    job: ReviewJob,
    hazards: Sequence[ReviewHazard],
    prerequisites: Sequence[ReviewPrerequisite],
    audit_events: Sequence[ReviewAuditEvent],
) -> SignoffSummary:
    """Compute sign-off eligibility and current sign-off state from job items and audit events."""
    blocking_reasons: list[str] = []
    if job.status != JobStatus.COMPLETED:
        blocking_reasons.append(f"Review job is {job.status}, not COMPLETED.")
    else:
        unacked_blocking = sum(
            1 for h in hazards if h.severity in BLOCKING_SEVERITIES and h.acknowledged_at is None
        )
        if unacked_blocking:
            noun = "hazard" if unacked_blocking == 1 else "hazards"
            blocking_reasons.append(f"{unacked_blocking} CRITICAL/HIGH {noun} unacknowledged")
        incomplete_prereqs = sum(1 for p in prerequisites if p.completed_at is None)
        if incomplete_prereqs:
            noun = "prerequisite" if incomplete_prereqs == 1 else "prerequisites"
            blocking_reasons.append(f"{incomplete_prereqs} deployment {noun} incomplete")

    can_sign_off = len(blocking_reasons) == 0

    latest_signoff: ReviewAuditEvent | None = None
    for event in audit_events:
        if event.event_type in (
            AuditEventType.RELEASE_SIGNED_OFF,
            AuditEventType.SIGNOFF_REVOKED,
        ) and (latest_signoff is None or event.created_at >= latest_signoff.created_at):
            latest_signoff = event

    is_signed_off = (
        can_sign_off
        and latest_signoff is not None
        and latest_signoff.event_type == AuditEventType.RELEASE_SIGNED_OFF
    )
    return SignoffSummary(
        is_signed_off=is_signed_off,
        signed_off_by=latest_signoff.actor_display if is_signed_off and latest_signoff else None,
        signed_off_at=latest_signoff.created_at if is_signed_off and latest_signoff else None,
        signoff_note=latest_signoff.note if is_signed_off and latest_signoff else None,
        can_sign_off=can_sign_off,
        blocking_reasons=blocking_reasons,
    )


async def _is_currently_signed_off(session: AsyncSession, job_id: uuid.UUID) -> bool:
    latest = await session.scalar(
        select(ReviewAuditEvent)
        .where(
            ReviewAuditEvent.review_job_id == job_id,
            ReviewAuditEvent.event_type.in_(
                (AuditEventType.RELEASE_SIGNED_OFF, AuditEventType.SIGNOFF_REVOKED)
            ),
        )
        .order_by(ReviewAuditEvent.created_at.desc(), ReviewAuditEvent.id.desc())
        .limit(1)
    )
    return latest is not None and latest.event_type == AuditEventType.RELEASE_SIGNED_OFF


async def acknowledge_hazard(
    session: AsyncSession,
    hazard: ReviewHazard,
    user: User,
    *,
    note: str | None = None,
) -> ReviewHazard:
    now = datetime.now(UTC)
    already_acked = hazard.acknowledged_at is not None
    hazard.acknowledged_by = user.id
    if not already_acked:
        hazard.acknowledged_at = now
        session.add(
            ReviewAuditEvent(
                review_job_id=hazard.review_job_id,
                actor_id=user.id,
                actor_display=_actor_display(user),
                event_type=AuditEventType.HAZARD_ACKNOWLEDGED,
                hazard_id=hazard.id,
                note=note,
                created_at=now,
            )
        )
    await session.flush()
    return hazard


async def unacknowledge_hazard(
    session: AsyncSession,
    hazard: ReviewHazard,
    user: User,
    *,
    note: str | None = None,
) -> ReviewHazard:
    now = datetime.now(UTC)
    was_acked = hazard.acknowledged_at is not None
    hazard.acknowledged_by = None
    hazard.acknowledged_at = None
    if was_acked:
        session.add(
            ReviewAuditEvent(
                review_job_id=hazard.review_job_id,
                actor_id=user.id,
                actor_display=_actor_display(user),
                event_type=AuditEventType.HAZARD_UNACKNOWLEDGED,
                hazard_id=hazard.id,
                note=note,
                created_at=now,
            )
        )
        if hazard.severity in BLOCKING_SEVERITIES and await _is_currently_signed_off(
            session, hazard.review_job_id
        ):
            session.add(
                ReviewAuditEvent(
                    review_job_id=hazard.review_job_id,
                    actor_id=user.id,
                    actor_display=_actor_display(user),
                    event_type=AuditEventType.SIGNOFF_REVOKED,
                    hazard_id=hazard.id,
                    note="Automatically revoked: CRITICAL/HIGH hazard unacknowledged",
                    created_at=now + timedelta(microseconds=1),
                )
            )
    await session.flush()
    return hazard


async def complete_prerequisite(
    session: AsyncSession,
    prereq: ReviewPrerequisite,
    user: User,
    *,
    note: str | None = None,
) -> ReviewPrerequisite:
    now = datetime.now(UTC)
    already_completed = prereq.completed_at is not None
    prereq.completed_by = user.id
    if not already_completed:
        prereq.completed_at = now
        session.add(
            ReviewAuditEvent(
                review_job_id=prereq.review_job_id,
                actor_id=user.id,
                actor_display=_actor_display(user),
                event_type=AuditEventType.PREREQUISITE_COMPLETED,
                prerequisite_id=prereq.id,
                note=note,
                created_at=now,
            )
        )
    await session.flush()
    return prereq


async def reopen_prerequisite(
    session: AsyncSession,
    prereq: ReviewPrerequisite,
    user: User,
    *,
    note: str | None = None,
) -> ReviewPrerequisite:
    now = datetime.now(UTC)
    was_completed = prereq.completed_at is not None
    prereq.completed_by = None
    prereq.completed_at = None
    if was_completed:
        session.add(
            ReviewAuditEvent(
                review_job_id=prereq.review_job_id,
                actor_id=user.id,
                actor_display=_actor_display(user),
                event_type=AuditEventType.PREREQUISITE_REOPENED,
                prerequisite_id=prereq.id,
                note=note,
                created_at=now,
            )
        )
        if await _is_currently_signed_off(session, prereq.review_job_id):
            session.add(
                ReviewAuditEvent(
                    review_job_id=prereq.review_job_id,
                    actor_id=user.id,
                    actor_display=_actor_display(user),
                    event_type=AuditEventType.SIGNOFF_REVOKED,
                    prerequisite_id=prereq.id,
                    note="Automatically revoked: deployment prerequisite reopened",
                    created_at=now + timedelta(microseconds=1),
                )
            )
    await session.flush()
    return prereq


async def load_review_components(
    session: AsyncSession, job_id: uuid.UUID
) -> tuple[list[ReviewHazard], list[ReviewPrerequisite], list[ReviewAuditEvent]]:
    hazards = list(
        (
            await session.scalars(select(ReviewHazard).where(ReviewHazard.review_job_id == job_id))
        ).all()
    )
    prereqs = list(
        (
            await session.scalars(
                select(ReviewPrerequisite)
                .where(ReviewPrerequisite.review_job_id == job_id)
                .order_by(ReviewPrerequisite.sort_order, ReviewPrerequisite.id)
            )
        ).all()
    )
    events = list(
        (
            await session.scalars(
                select(ReviewAuditEvent)
                .where(ReviewAuditEvent.review_job_id == job_id)
                .order_by(ReviewAuditEvent.created_at, ReviewAuditEvent.id)
            )
        ).all()
    )
    return hazards, prereqs, events


async def sign_off_review(
    session: AsyncSession,
    job: ReviewJob,
    user: User,
    *,
    note: str | None = None,
) -> SignoffSummary:
    hazards, prereqs, events = await load_review_components(session, job.id)
    summary = compute_signoff_summary(job, hazards, prereqs, events)
    if not summary.can_sign_off:
        raise SignoffRuleError("Cannot sign off release: " + "; ".join(summary.blocking_reasons))
    cleaned_note = note.strip() if note else None
    if job.verdict == Verdict.BLOCKED and (
        cleaned_note is None or len(cleaned_note) < MIN_BLOCKED_OVERRIDE_NOTE_LEN
    ):
        raise SignoffRuleError(
            f"Signing off a BLOCKED release requires an override note of at least "
            f"{MIN_BLOCKED_OVERRIDE_NOTE_LEN} characters."
        )
    event = ReviewAuditEvent(
        review_job_id=job.id,
        actor_id=user.id,
        actor_display=_actor_display(user),
        event_type=AuditEventType.RELEASE_SIGNED_OFF,
        note=cleaned_note,
        created_at=datetime.now(UTC),
    )
    session.add(event)
    await session.flush()
    return compute_signoff_summary(job, hazards, prereqs, [*events, event])


async def revoke_sign_off(
    session: AsyncSession,
    job: ReviewJob,
    user: User,
    *,
    note: str | None = None,
) -> SignoffSummary:
    hazards, prereqs, events = await load_review_components(session, job.id)
    summary = compute_signoff_summary(job, hazards, prereqs, events)
    if summary.is_signed_off:
        event = ReviewAuditEvent(
            review_job_id=job.id,
            actor_id=user.id,
            actor_display=_actor_display(user),
            event_type=AuditEventType.SIGNOFF_REVOKED,
            note=note.strip() if note else None,
            created_at=datetime.now(UTC),
        )
        session.add(event)
        await session.flush()
        events = [*events, event]
    return compute_signoff_summary(job, hazards, prereqs, events)
