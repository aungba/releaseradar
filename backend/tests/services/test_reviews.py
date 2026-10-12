"""run_review against a real database, a fake GitHub API and a fake LLM."""

import uuid
from collections.abc import AsyncIterator, Callable

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.domain.enums import GitProviderKind, JobStatus, PrerequisiteKind, Verdict
from app.engine.llm import FakeLLMClient, LLMError
from app.engine.models import RawMetrics
from app.engine.pipeline import EngineSettings
from app.integrations.git import GitHubProvider, GitProvider
from app.models import GitConnection, Organization, Project
from app.models.review import ReviewHazard, ReviewJob, ReviewPrerequisite
from app.services.reviews import create_review_job, run_review
from tests.engine.helpers import hazard, report_json
from tests.integrations import fakes

pytestmark = pytest.mark.db

SETTINGS = EngineSettings(primary_model="gemini/test", fallback_model="groq/test")
REPO_PATH = f"/api/v3/repos/{fakes.GITHUB_REPO}"


@pytest.fixture
async def sessionmaker(migrated_database: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions share one connection whose outer transaction is rolled back after the test."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    async with engine.connect() as conn:
        trans = await conn.begin()
        yield async_sessionmaker(
            bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False
        )
        await trans.rollback()
    await engine.dispose()


@pytest.fixture
async def job_id(sessionmaker: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with sessionmaker() as s, s.begin():
        org = Organization(name="Acme")
        s.add(org)
        await s.flush()
        conn = GitConnection(
            org_id=org.id, provider=GitProviderKind.GITHUB, base_url=fakes.GITHUB_API,
            account_login="acme", access_token_encrypted="unused-by-fake-factory",
        )  # fmt: skip
        s.add(conn)
        await s.flush()
        project = Project(
            org_id=org.id, git_connection_id=conn.id, name="api", repo_identifier=fakes.GITHUB_REPO
        )
        s.add(project)
        await s.flush()
        job = await create_review_job(s, project.id, "v1", "v2")
        return job.id


def github_factory(
    clients: list[httpx.AsyncClient], extra: dict[str, fakes.Handler] | None = None
) -> Callable[[GitConnection], GitProvider]:
    routes: dict[str, fakes.Handler] = {
        f"{REPO_PATH}/commits/v1": lambda _r: httpx.Response(200, text=fakes.BASE_SHA),
        f"{REPO_PATH}/commits/v2": lambda _r: httpx.Response(200, text=fakes.HEAD_SHA),
        **(extra or {}),
    }

    def factory(_connection: GitConnection) -> GitProvider:
        handler = fakes.github_handler(fakes.Recorder(), extra=routes)
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clients.append(client)
        return GitHubProvider(fakes.TOKEN, fakes.GITHUB_API, client=client)

    return factory


@pytest.fixture
async def clients() -> AsyncIterator[list[httpx.AsyncClient]]:
    created: list[httpx.AsyncClient] = []
    yield created
    for client in created:
        await client.aclose()


async def load(sm: async_sessionmaker[AsyncSession], job_id: uuid.UUID) -> ReviewJob:
    async with sm() as s:
        return await s.get_one(ReviewJob, job_id, populate_existing=True)


async def test_completed_review_is_persisted(
    sessionmaker: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID,
    clients: list[httpx.AsyncClient],
) -> None:
    llm = FakeLLMClient(
        [report_json(hazards=[hazard("src/app.py", breaking=True, symbol="x")], env_vars=["STRIPE_KEY"],
                     actions=["Warm the cache"])]
    )  # fmt: skip
    status = await run_review(
        job_id, sessionmaker=sessionmaker, llm=llm, engine_settings=SETTINGS,
        provider_factory=github_factory(clients),
    )  # fmt: skip

    assert status is JobStatus.COMPLETED
    job = await load(sessionmaker, job_id)
    assert job.status == JobStatus.COMPLETED
    assert (job.base_sha, job.target_sha) == (fakes.BASE_SHA, fakes.HEAD_SHA)
    assert job.verdict == Verdict.APPROVED_WITH_CAUTION and job.risk_score == 45
    assert job.llm_model == "gemini/test" and job.completed_at and job.started_at
    metrics = RawMetrics.model_validate(job.raw_metrics)
    assert metrics.files_total == 7 and metrics.latency_ms.git is not None

    async with sessionmaker() as s:
        hazards = (
            await s.scalars(select(ReviewHazard).where(ReviewHazard.review_job_id == job_id))
        ).all()
        prereqs = (
            await s.scalars(
                select(ReviewPrerequisite)
                .where(ReviewPrerequisite.review_job_id == job_id)
                .order_by(ReviewPrerequisite.sort_order)
            )
        ).all()
    assert [(h.file_path, h.is_breaking, h.affected_symbol) for h in hazards] == [
        ("src/app.py", True, "x")
    ]
    assert [(p.kind, p.description) for p in prereqs] == [
        (PrerequisiteKind.REQUIRED_ENV_VAR, "STRIPE_KEY"),
        (PrerequisiteKind.PRE_DEPLOY_ACTION, "Warm the cache"),
    ]


async def test_rerun_replaces_previous_results(
    sessionmaker: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID,
    clients: list[httpx.AsyncClient],
) -> None:
    kwargs = {
        "sessionmaker": sessionmaker,
        "engine_settings": SETTINGS,
        "provider_factory": github_factory(clients),
    }
    two = report_json(hazards=[hazard("src/app.py", title="a"), hazard("src/new.py", title="b")])
    await run_review(job_id, llm=FakeLLMClient([two]), **kwargs)  # type: ignore[arg-type]
    async with sessionmaker() as s, s.begin():
        (await s.get_one(ReviewJob, job_id)).status = JobStatus.QUEUED
    await run_review(job_id, llm=FakeLLMClient([report_json()]), **kwargs)  # type: ignore[arg-type]

    async with sessionmaker() as s:
        count = len(
            (
                await s.scalars(select(ReviewHazard).where(ReviewHazard.review_job_id == job_id))
            ).all()
        )
    assert count == 0


async def test_only_queued_jobs_run(
    sessionmaker: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID,
    clients: list[httpx.AsyncClient],
) -> None:
    async with sessionmaker() as s, s.begin():
        (await s.get_one(ReviewJob, job_id)).status = JobStatus.CANCELLED
    llm = FakeLLMClient([])
    status = await run_review(
        job_id, sessionmaker=sessionmaker, llm=llm, engine_settings=SETTINGS,
        provider_factory=github_factory(clients),
    )  # fmt: skip
    assert status is None and llm.calls == []


@pytest.mark.parametrize(
    ("route_status", "expected"),
    [
        (401, "Git access token is invalid, expired or revoked"),
        (403, "Git access token lacks access to this repository."),
        (404, "Repository or ref not found"),
        (500, "Git provider error"),
    ],
)
async def test_git_failures_fail_the_job_with_safe_message(
    sessionmaker: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID,
    clients: list[httpx.AsyncClient],
    route_status: int,
    expected: str,
) -> None:
    extra: dict[str, fakes.Handler] = {
        f"{REPO_PATH}/commits/v1": lambda _r: httpx.Response(route_status, json={"message": "x"})
    }
    status = await run_review(
        job_id, sessionmaker=sessionmaker, llm=FakeLLMClient([]), engine_settings=SETTINGS,
        provider_factory=github_factory(clients, extra),
    )  # fmt: skip
    job = await load(sessionmaker, job_id)
    assert status is JobStatus.FAILED and job.status == JobStatus.FAILED
    assert job.error_message is not None and job.error_message.startswith(expected)
    assert fakes.TOKEN not in job.error_message


async def test_llm_failure_fails_the_job(
    sessionmaker: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID,
    clients: list[httpx.AsyncClient],
) -> None:
    llm = FakeLLMClient([LLMError("gemini/test: Timeout"), LLMError("groq/test: RateLimitError")])
    await run_review(
        job_id, sessionmaker=sessionmaker, llm=llm, engine_settings=SETTINGS,
        provider_factory=github_factory(clients),
    )  # fmt: skip
    job = await load(sessionmaker, job_id)
    assert job.status == JobStatus.FAILED
    assert job.error_message == (
        "All LLM providers failed: gemini/test: Timeout; groq/test: RateLimitError"
    )
    assert job.base_sha == fakes.BASE_SHA  # resolved before the failure


async def test_rate_limit_requeues_for_worker_retry(
    sessionmaker: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID,
    clients: list[httpx.AsyncClient],
) -> None:
    extra: dict[str, fakes.Handler] = {
        f"{REPO_PATH}/commits/v1": lambda _r: httpx.Response(429, headers={"retry-after": "30"})
    }
    from app.integrations.git import RateLimitError

    with pytest.raises(RateLimitError):
        await run_review(
            job_id, sessionmaker=sessionmaker, llm=FakeLLMClient([]), engine_settings=SETTINGS,
            provider_factory=github_factory(clients, extra), requeue_on_rate_limit=True,
        )  # fmt: skip
    job = await load(sessionmaker, job_id)
    assert job.status == JobStatus.QUEUED and job.started_at is None

    status = await run_review(
        job_id, sessionmaker=sessionmaker, llm=FakeLLMClient([]), engine_settings=SETTINGS,
        provider_factory=github_factory(clients, extra), requeue_on_rate_limit=False,
    )  # fmt: skip
    assert status is JobStatus.FAILED
    assert (await load(sessionmaker, job_id)).error_message == (
        "Git provider rate limit exceeded. Try again later."
    )


async def test_unexpected_error_gets_generic_message(
    sessionmaker: async_sessionmaker[AsyncSession], job_id: uuid.UUID
) -> None:
    def broken(_connection: GitConnection) -> GitProvider:
        raise RuntimeError("secret internals")

    await run_review(
        job_id, sessionmaker=sessionmaker, llm=FakeLLMClient([]), engine_settings=SETTINGS,
        provider_factory=broken,
    )  # fmt: skip
    message = (await load(sessionmaker, job_id)).error_message
    assert message == "Internal error during analysis. The team has been notified."
