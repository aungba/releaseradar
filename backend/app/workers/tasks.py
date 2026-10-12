import asyncio
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.domain.enums import JobStatus
from app.engine.pipeline import EngineSettings
from app.integrations.git import RateLimitError
from app.services.reviews import build_llm_client, run_review
from app.workers.celery_app import celery_app

DEFAULT_RATE_LIMIT_RETRY_SECONDS = 60


@celery_app.task(name="releaseradar.ping")  # type: ignore[untyped-decorator]
def ping() -> str:
    """Smoke-test task."""
    return "pong"


async def _run(job_id: uuid.UUID, *, requeue_on_rate_limit: bool) -> JobStatus | None:
    settings = get_settings()
    # A fresh engine per task: asyncio.run() creates a new event loop each time and pooled
    # asyncpg connections cannot cross loops.
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        return await run_review(
            job_id,
            sessionmaker=async_sessionmaker(engine, expire_on_commit=False),
            llm=build_llm_client(settings),
            engine_settings=EngineSettings.from_settings(settings),
            requeue_on_rate_limit=requeue_on_rate_limit,
        )
    finally:
        await engine.dispose()


@celery_app.task(  # type: ignore[untyped-decorator]
    name="releaseradar.run_review", bind=True, max_retries=3, acks_late=True
)
def run_review_task(self: Any, job_id: str) -> str | None:
    """Run a QUEUED review job. Git rate limits are retried up to ``max_retries`` times."""
    can_retry = self.request.retries < self.max_retries
    try:
        status = asyncio.run(_run(uuid.UUID(job_id), requeue_on_rate_limit=can_retry))
    except RateLimitError as exc:
        countdown = exc.retry_after_seconds or DEFAULT_RATE_LIMIT_RETRY_SECONDS
        raise self.retry(countdown=countdown, exc=exc) from exc
    return status.value if status else None
