"""Running a review job end to end: git → engine → database (M2 plan §7)."""

import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.domain.enums import JobStatus, PrerequisiteKind, ReviewType
from app.engine.llm import LiteLLMClient, LLMClient
from app.engine.models import AnalysisResult, ReviewContext
from app.engine.pipeline import EngineError, EngineSettings, analyze
from app.integrations.git import (
    AccessDeniedError,
    AuthenticationError,
    GitProvider,
    GitProviderError,
    NotFoundError,
    RateLimitError,
    UnsupportedProviderError,
    build_provider,
)
from app.models.git import GitConnection, Project
from app.models.review import ReviewHazard, ReviewJob, ReviewPrerequisite

logger = logging.getLogger(__name__)

ProviderFactory = Callable[[GitConnection], GitProvider]


def build_llm_client(settings: Settings) -> LiteLLMClient:
    return LiteLLMClient(
        api_keys={"gemini": settings.gemini_api_key, "groq": settings.groq_api_key},
        timeout_seconds=settings.llm_timeout_seconds,
        max_output_tokens=settings.llm_max_output_tokens,
    )


async def create_review_job(
    session: AsyncSession,
    project_id: uuid.UUID,
    base_ref: str,
    target_ref: str,
    *,
    triggered_by: uuid.UUID | None = None,
) -> ReviewJob:
    job = ReviewJob(
        project_id=project_id,
        triggered_by=triggered_by,
        review_type=ReviewType.ON_DEMAND_COMPARE,
        status=JobStatus.QUEUED,
        base_ref=base_ref,
        target_ref=target_ref,
    )
    session.add(job)
    await session.flush()
    return job


def failure_message(exc: BaseException) -> str:
    """User-facing, secret-free error text for ``review_jobs.error_message``."""
    if isinstance(exc, AuthenticationError):
        return (
            "Git access token is invalid, expired or revoked. Update the project's git connection."
        )
    if isinstance(exc, AccessDeniedError):
        return "Git access token lacks access to this repository."
    if isinstance(exc, RateLimitError):
        return "Git provider rate limit exceeded. Try again later."
    if isinstance(exc, NotFoundError):
        return f"Repository or ref not found ({exc})."
    if isinstance(exc, UnsupportedProviderError | EngineError):
        return str(exc)
    if isinstance(exc, GitProviderError):
        return f"Git provider error ({exc})."
    return "Internal error during analysis. The team has been notified."


async def run_review(
    job_id: uuid.UUID,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    llm: LLMClient,
    engine_settings: EngineSettings,
    provider_factory: ProviderFactory = build_provider,
    requeue_on_rate_limit: bool = False,
) -> JobStatus | None:
    """Run one QUEUED job. Returns the final status, or None if the job was not runnable.

    With ``requeue_on_rate_limit`` a git ``RateLimitError`` puts the job back to QUEUED and is
    re-raised so the worker can retry later (``retry_after_seconds``).
    """
    started = time.monotonic()
    async with sessionmaker() as session, session.begin():
        job = await session.scalar(
            select(ReviewJob)
            .where(ReviewJob.id == job_id, ReviewJob.status == JobStatus.QUEUED)
            .with_for_update(skip_locked=True)
        )
        if job is None:
            return None
        project = await session.get_one(Project, job.project_id)
        connection = await session.get_one(GitConnection, project.git_connection_id)
        job.status = JobStatus.RUNNING
        job.started_at = datetime.now(UTC)
        repo, base_ref, target_ref = project.repo_identifier, job.base_ref, job.target_ref
        session.expunge(connection)  # used after commit, must not be expired

    base_sha: str | None = None
    target_sha: str | None = None
    try:
        git_started = time.monotonic()
        async with provider_factory(connection) as git:
            base_sha = await git.resolve_ref(repo, base_ref)
            target_sha = await git.resolve_ref(repo, target_ref)
            compare = await git.compare(repo, base_sha, target_sha)
        git_ms = int((time.monotonic() - git_started) * 1000)
        context = ReviewContext(
            repo=repo, base_ref=base_ref, target_ref=target_ref, commits=tuple(compare.commits)
        )
        result = await analyze(compare, context, llm, engine_settings)
        result.metrics.latency_ms.git = git_ms
        result.metrics.latency_ms.total = int((time.monotonic() - started) * 1000)
    except RateLimitError:
        if requeue_on_rate_limit:
            await _requeue(sessionmaker, job_id)
            raise
        return await _fail(
            sessionmaker, job_id, failure_message(RateLimitError("")), base_sha, target_sha
        )
    except Exception as exc:
        if not isinstance(exc, GitProviderError | EngineError):
            logger.exception("review %s failed unexpectedly", job_id)
        return await _fail(sessionmaker, job_id, failure_message(exc), base_sha, target_sha)

    await _complete(sessionmaker, job_id, result, base_sha, target_sha)
    return JobStatus.COMPLETED


async def _complete(
    sessionmaker: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID,
    result: AnalysisResult,
    base_sha: str | None,
    target_sha: str | None,
) -> None:
    report = result.report
    prereqs = report.deployment_prerequisites
    async with sessionmaker() as session, session.begin():
        job = await session.get_one(ReviewJob, job_id, with_for_update=True)
        # Idempotent: a re-run replaces earlier results.
        await session.execute(delete(ReviewHazard).where(ReviewHazard.review_job_id == job_id))
        await session.execute(
            delete(ReviewPrerequisite).where(ReviewPrerequisite.review_job_id == job_id)
        )
        session.add_all(
            ReviewHazard(
                review_job_id=job_id,
                category=h.category,
                severity=h.severity,
                file_path=h.file_path,
                hazard_title=h.hazard_title,
                explanation=h.explanation,
                remediation=h.remediation,
                is_breaking=h.is_breaking,
                change_type=h.change_type,
                affected_symbol=h.affected_symbol,
            )
            for h in report.hazards
        )
        items = [(PrerequisiteKind.REQUIRED_ENV_VAR, v) for v in prereqs.required_env_vars] + [
            (PrerequisiteKind.PRE_DEPLOY_ACTION, a) for a in prereqs.pre_deploy_actions
        ]
        session.add_all(
            ReviewPrerequisite(review_job_id=job_id, kind=kind, description=text, sort_order=i)
            for i, (kind, text) in enumerate(items)
        )
        job.base_sha, job.target_sha = base_sha, target_sha
        job.verdict = report.verdict
        job.risk_score = report.risk_score
        job.executive_summary = report.executive_summary
        job.rollback_safe = prereqs.zero_downtime_rollback_safe
        job.rollback_notes = prereqs.rollback_notes
        job.llm_model = result.metrics.model
        job.raw_metrics = result.metrics.model_dump(mode="json")
        job.error_message = None
        job.status = JobStatus.COMPLETED
        job.completed_at = datetime.now(UTC)


async def _fail(
    sessionmaker: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID,
    message: str,
    base_sha: str | None,
    target_sha: str | None,
) -> JobStatus:
    async with sessionmaker() as session, session.begin():
        job = await session.get_one(ReviewJob, job_id, with_for_update=True)
        job.status = JobStatus.FAILED
        job.error_message = message
        job.base_sha, job.target_sha = base_sha, target_sha
        job.completed_at = datetime.now(UTC)
    return JobStatus.FAILED


async def _requeue(sessionmaker: async_sessionmaker[AsyncSession], job_id: uuid.UUID) -> None:
    async with sessionmaker() as session, session.begin():
        job = await session.get_one(ReviewJob, job_id, with_for_update=True)
        job.status = JobStatus.QUEUED
        job.started_at = None
