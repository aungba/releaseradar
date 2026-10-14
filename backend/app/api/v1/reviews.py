"""Reviews endpoints (/api/v1/reviews)."""

import uuid
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.projects import get_org_project
from app.core.auth import CurrentAuth
from app.core.db import get_session
from app.domain.enums import JobStatus, Severity, Verdict, risk_rating_for
from app.models.git import Project
from app.models.review import ReviewHazard, ReviewJob
from app.schemas.api import (
    CreateReviewRequest,
    HazardResponse,
    PrerequisiteResponse,
    ReviewDetailResponse,
    ReviewJobSummary,
)
from app.services.reviews import create_review_job
from app.services.signoff import compute_signoff_summary, load_review_components

router = APIRouter(prefix="/reviews", tags=["reviews"])

_SEVERITY_SORT_RANK: dict[str, int] = {
    Severity.CRITICAL: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
}

ReviewEnqueuer = Callable[[uuid.UUID], None]


def _celery_enqueue(job_id: uuid.UUID) -> None:
    from app.workers.tasks import run_review_task

    run_review_task.delay(str(job_id))


def get_review_enqueuer() -> ReviewEnqueuer:
    """Injectable enqueuer for tests."""
    return _celery_enqueue


def _to_job_summary(job: ReviewJob) -> ReviewJobSummary:
    verdict = Verdict(job.verdict) if job.verdict is not None else None
    return ReviewJobSummary(
        id=job.id,
        project_id=job.project_id,
        triggered_by=job.triggered_by,
        review_type=job.review_type,
        status=job.status,
        base_ref=job.base_ref,
        target_ref=job.target_ref,
        base_sha=job.base_sha,
        target_sha=job.target_sha,
        pr_number=job.pr_number,
        verdict=verdict,
        risk_rating=risk_rating_for(verdict) if verdict is not None else None,
        risk_score=job.risk_score,
        executive_summary=job.executive_summary,
        rollback_safe=job.rollback_safe,
        rollback_notes=job.rollback_notes,
        llm_model=job.llm_model,
        error_message=job.error_message,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
    )


def _sort_hazards(hazards: list[ReviewHazard]) -> list[ReviewHazard]:
    return sorted(
        hazards,
        key=lambda h: (
            _SEVERITY_SORT_RANK.get(h.severity, 99),
            0 if h.is_breaking else 1,
            h.file_path,
            str(h.id),
        ),
    )


async def get_org_review_job(
    session: AsyncSession, review_id: uuid.UUID, org_id: uuid.UUID
) -> ReviewJob:
    job = await session.scalar(
        select(ReviewJob)
        .join(Project, ReviewJob.project_id == Project.id)
        .where(ReviewJob.id == review_id, Project.org_id == org_id)
    )
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review job not found.")
    return job


@router.post("", response_model=ReviewJobSummary, status_code=status.HTTP_202_ACCEPTED)
async def start_review(
    body: CreateReviewRequest,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session)],
    enqueue: Annotated[ReviewEnqueuer, Depends(get_review_enqueuer)],
) -> ReviewJobSummary:
    """Create a QUEUED review job and enqueue it for asynchronous analysis."""
    if body.base_ref == body.target_ref:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="base_ref and target_ref must be different.",
        )
    project, _ = await get_org_project(session, body.project_id, ctx.org.id)
    job = await create_review_job(
        session,
        project.id,
        body.base_ref,
        body.target_ref,
        triggered_by=ctx.user.id,
    )
    await session.commit()
    await session.refresh(job)
    enqueue(job.id)
    return _to_job_summary(job)


@router.get("", response_model=list[ReviewJobSummary])
async def list_reviews(
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session)],
    project_id: Annotated[uuid.UUID | None, Query()] = None,
    status_filter: Annotated[JobStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ReviewJobSummary]:
    """List review jobs in the current organization (newest first)."""
    stmt = (
        select(ReviewJob)
        .join(Project, ReviewJob.project_id == Project.id)
        .where(Project.org_id == ctx.org.id)
    )
    if project_id is not None:
        stmt = stmt.where(ReviewJob.project_id == project_id)
    if status_filter is not None:
        stmt = stmt.where(ReviewJob.status == status_filter)
    stmt = (
        stmt.order_by(ReviewJob.created_at.desc(), ReviewJob.id.desc()).limit(limit).offset(offset)
    )
    rows = (await session.scalars(stmt)).all()
    return [_to_job_summary(job) for job in rows]


@router.get("/{review_id}", response_model=ReviewDetailResponse)
async def get_review(
    review_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ReviewDetailResponse:
    """Return a review job with its hazards, prerequisites, and sign-off status."""
    job = await get_org_review_job(session, review_id, ctx.org.id)
    hazards, prereqs, events = await load_review_components(session, job.id)
    sorted_hazards = _sort_hazards(hazards)
    signoff_status = compute_signoff_summary(job, sorted_hazards, prereqs, events)
    summary = _to_job_summary(job)
    return ReviewDetailResponse(
        **summary.model_dump(),
        raw_metrics=job.raw_metrics or {},
        hazards=[HazardResponse.model_validate(h) for h in sorted_hazards],
        prerequisites=[PrerequisiteResponse.model_validate(p) for p in prereqs],
        signoff_status=signoff_status,
    )
