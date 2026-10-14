"""Interactive hazard acknowledgement, prerequisite completion, and release sign-off endpoints."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.reviews import get_org_review_job
from app.core.auth import CurrentAuth
from app.core.db import get_session
from app.models.git import Project
from app.models.review import ReviewAuditEvent, ReviewHazard, ReviewJob, ReviewPrerequisite
from app.schemas.api import (
    AuditEventResponse,
    HazardResponse,
    NoteRequest,
    PrerequisiteResponse,
    SignoffSummary,
)
from app.services.signoff import (
    SignoffRuleError,
    acknowledge_hazard,
    complete_prerequisite,
    reopen_prerequisite,
    revoke_sign_off,
    sign_off_review,
    unacknowledge_hazard,
)

router = APIRouter(tags=["signoff"])


async def _get_org_hazard(
    session: AsyncSession, hazard_id: uuid.UUID, org_id: uuid.UUID
) -> ReviewHazard:
    hazard = await session.scalar(
        select(ReviewHazard)
        .join(ReviewJob, ReviewHazard.review_job_id == ReviewJob.id)
        .join(Project, ReviewJob.project_id == Project.id)
        .where(ReviewHazard.id == hazard_id, Project.org_id == org_id)
    )
    if hazard is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Hazard not found.")
    return hazard


async def _get_org_prerequisite(
    session: AsyncSession, prerequisite_id: uuid.UUID, org_id: uuid.UUID
) -> ReviewPrerequisite:
    prereq = await session.scalar(
        select(ReviewPrerequisite)
        .join(ReviewJob, ReviewPrerequisite.review_job_id == ReviewJob.id)
        .join(Project, ReviewJob.project_id == Project.id)
        .where(ReviewPrerequisite.id == prerequisite_id, Project.org_id == org_id)
    )
    if prereq is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Prerequisite not found.")
    return prereq


def _resolve_note(body: NoteRequest | None, query_note: str | None) -> str | None:
    if body is not None and body.note is not None:
        return body.note
    return query_note


@router.put("/hazards/{hazard_id}/acknowledgement", response_model=HazardResponse)
async def acknowledge_hazard_endpoint(
    hazard_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session)],
    body: Annotated[NoteRequest | None, Body()] = None,
) -> HazardResponse:
    """Acknowledge a flagged hazard and record an audit event."""
    hazard = await _get_org_hazard(session, hazard_id, ctx.org.id)
    await acknowledge_hazard(session, hazard, ctx.user, note=body.note if body else None)
    await session.commit()
    await session.refresh(hazard)
    return HazardResponse.model_validate(hazard)


@router.delete("/hazards/{hazard_id}/acknowledgement", response_model=HazardResponse)
async def unacknowledge_hazard_endpoint(
    hazard_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session)],
    body: Annotated[NoteRequest | None, Body()] = None,
    note: Annotated[str | None, Query(max_length=2000)] = None,
) -> HazardResponse:
    """Un-acknowledge a hazard. Automatically revokes release sign-off if CRITICAL or HIGH."""
    hazard = await _get_org_hazard(session, hazard_id, ctx.org.id)
    await unacknowledge_hazard(session, hazard, ctx.user, note=_resolve_note(body, note))
    await session.commit()
    await session.refresh(hazard)
    return HazardResponse.model_validate(hazard)


@router.put("/prerequisites/{prerequisite_id}/completion", response_model=PrerequisiteResponse)
async def complete_prerequisite_endpoint(
    prerequisite_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session)],
    body: Annotated[NoteRequest | None, Body()] = None,
) -> PrerequisiteResponse:
    """Mark a deployment prerequisite as completed and record an audit event."""
    prereq = await _get_org_prerequisite(session, prerequisite_id, ctx.org.id)
    await complete_prerequisite(session, prereq, ctx.user, note=body.note if body else None)
    await session.commit()
    await session.refresh(prereq)
    return PrerequisiteResponse.model_validate(prereq)


@router.delete("/prerequisites/{prerequisite_id}/completion", response_model=PrerequisiteResponse)
async def reopen_prerequisite_endpoint(
    prerequisite_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session)],
    body: Annotated[NoteRequest | None, Body()] = None,
    note: Annotated[str | None, Query(max_length=2000)] = None,
) -> PrerequisiteResponse:
    """Reopen a completed deployment prerequisite. Automatically revokes release sign-off."""
    prereq = await _get_org_prerequisite(session, prerequisite_id, ctx.org.id)
    await reopen_prerequisite(session, prereq, ctx.user, note=_resolve_note(body, note))
    await session.commit()
    await session.refresh(prereq)
    return PrerequisiteResponse.model_validate(prereq)


@router.post("/reviews/{review_id}/signoff", response_model=SignoffSummary)
async def sign_off_review_endpoint(
    review_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session)],
    body: Annotated[NoteRequest | None, Body()] = None,
) -> SignoffSummary:
    """Sign off on a completed review once all CRITICAL/HIGH hazards and prerequisites are clear."""
    job = await get_org_review_job(session, review_id, ctx.org.id)
    try:
        summary = await sign_off_review(session, job, ctx.user, note=body.note if body else None)
    except SignoffRuleError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    await session.commit()
    return summary


@router.delete("/reviews/{review_id}/signoff", response_model=SignoffSummary)
async def revoke_sign_off_endpoint(
    review_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session)],
    body: Annotated[NoteRequest | None, Body()] = None,
    note: Annotated[str | None, Query(max_length=2000)] = None,
) -> SignoffSummary:
    """Revoke an existing release sign-off and append a SIGNOFF_REVOKED audit event."""
    job = await get_org_review_job(session, review_id, ctx.org.id)
    summary = await revoke_sign_off(session, job, ctx.user, note=_resolve_note(body, note))
    await session.commit()
    return summary


@router.get("/reviews/{review_id}/audit", response_model=list[AuditEventResponse])
async def list_review_audit_events(
    review_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[AuditEventResponse]:
    """List chronological audit trail events for a review job."""
    job = await get_org_review_job(session, review_id, ctx.org.id)
    events = (
        await session.scalars(
            select(ReviewAuditEvent)
            .where(ReviewAuditEvent.review_job_id == job.id)
            .order_by(ReviewAuditEvent.created_at.asc(), ReviewAuditEvent.id.asc())
        )
    ).all()
    return [AuditEventResponse.model_validate(e) for e in events]
