"""Database constraint tests: every rule from PRD §5 is enforced by PostgreSQL itself."""

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import generate_api_token
from app.domain.enums import (
    ChangeType,
    GitAuthType,
    GitProviderKind,
    HazardCategory,
    JobStatus,
    ReviewType,
    Severity,
    Verdict,
)
from app.models import (
    ApiToken,
    Base,
    DailyDigest,
    GitConnection,
    Organization,
    Project,
    ReviewHazard,
    ReviewJob,
    User,
)

pytestmark = pytest.mark.db


# ---------- helpers ----------


async def add(session: AsyncSession, *objs: Any) -> None:
    session.add_all(objs)
    await session.flush()


async def assert_violates(session: AsyncSession, obj: Any, constraint: str) -> None:
    """Insert ``obj`` inside a savepoint and assert it fails on the named constraint."""
    with pytest.raises(IntegrityError) as exc_info:
        async with session.begin_nested():
            session.add(obj)
            await session.flush()
    assert constraint in str(exc_info.value.orig)


async def make_org(session: AsyncSession, name: str = "Acme") -> Organization:
    org = Organization(name=name)
    await add(session, org)
    return org


async def make_connection(session: AsyncSession, org: Organization) -> GitConnection:
    conn = GitConnection(
        org_id=org.id,
        provider=GitProviderKind.GITHUB,
        base_url="https://api.github.com",
        account_login="acme",
        access_token_encrypted="ciphertext",
    )
    await add(session, conn)
    return conn


async def make_project(
    session: AsyncSession, repo: str = "acme/payments"
) -> tuple[Organization, Project]:
    org = await make_org(session)
    conn = await make_connection(session, org)
    project = Project(
        org_id=org.id, git_connection_id=conn.id, name="payments", repo_identifier=repo
    )
    await add(session, project)
    return org, project


def new_job(project: Project, **overrides: Any) -> ReviewJob:
    fields: dict[str, Any] = {
        "project_id": project.id,
        "review_type": ReviewType.ON_DEMAND_COMPARE,
        "base_ref": "v2.4.0",
        "target_ref": "rc-v2.5.0",
    }
    fields.update(overrides)
    return ReviewJob(**fields)


def completed_fields() -> dict[str, Any]:
    return {
        "status": JobStatus.COMPLETED,
        "verdict": Verdict.APPROVED,
        "risk_score": 10,
        "executive_summary": "Small, safe change.",
        "rollback_safe": True,
        "completed_at": datetime.now(UTC),
    }


def new_hazard(job: ReviewJob, **overrides: Any) -> ReviewHazard:
    fields: dict[str, Any] = {
        "review_job_id": job.id,
        "category": HazardCategory.DATABASE,
        "severity": Severity.HIGH,
        "file_path": "migrations/0042_drop_column.sql",
        "hazard_title": "Column dropped",
        "explanation": "Old pods still read the column.",
        "remediation": "Deploy code first, drop column next release.",
    }
    fields.update(overrides)
    return ReviewHazard(**fields)


# ---------- migrations ----------


async def test_migrations_match_models(session: AsyncSession) -> None:
    """Fails if someone changes a model without generating a migration."""
    conn = await session.connection()
    diff = await conn.run_sync(
        lambda sync_conn: compare_metadata(MigrationContext.configure(sync_conn), Base.metadata)
    )
    assert diff == []


async def test_server_defaults_applied(session: AsyncSession) -> None:
    org = await make_org(session)
    await session.refresh(org)
    assert org.timezone == "UTC"
    assert org.digest_local_time.hour == 18


# ---------- review_jobs ----------


async def test_queued_job_without_result_is_allowed(session: AsyncSession) -> None:
    _, project = await make_project(session)
    job = new_job(project)
    await add(session, job)
    await session.refresh(job)
    assert job.status == JobStatus.QUEUED
    assert job.verdict is None
    assert job.raw_metrics == {}


async def test_completed_job_requires_result(session: AsyncSession) -> None:
    _, project = await make_project(session)
    await assert_violates(
        session, new_job(project, status=JobStatus.COMPLETED), "completed_requires_result"
    )


async def test_completed_job_with_result_is_allowed(session: AsyncSession) -> None:
    _, project = await make_project(session)
    await add(session, new_job(project, **completed_fields()))


async def test_failed_job_requires_error_message(session: AsyncSession) -> None:
    _, project = await make_project(session)
    await assert_violates(
        session, new_job(project, status=JobStatus.FAILED), "failed_requires_error"
    )


async def test_risk_score_range(session: AsyncSession) -> None:
    _, project = await make_project(session)
    fields = completed_fields() | {"risk_score": 101}
    await assert_violates(session, new_job(project, **fields), "risk_score_range")


async def test_invalid_verdict_rejected(session: AsyncSession) -> None:
    _, project = await make_project(session)
    fields = completed_fields() | {"verdict": "CAUTION"}
    await assert_violates(session, new_job(project, **fields), "ck_review_jobs_verdict")


async def test_digest_job_requires_digest(session: AsyncSession) -> None:
    _, project = await make_project(session)
    job = new_job(project, review_type=ReviewType.DAILY_DIGEST)
    await assert_violates(session, job, "digest_type_requires_digest")


async def test_pr_job_requires_pr_number(session: AsyncSession) -> None:
    _, project = await make_project(session)
    job = new_job(project, review_type=ReviewType.PULL_REQUEST)
    await assert_violates(session, job, "pr_type_requires_pr_number")


# ---------- review_hazards ----------


async def test_shared_state_category_accepted(session: AsyncSession) -> None:
    _, project = await make_project(session)
    job = new_job(project)
    await add(session, job)
    await add(session, new_hazard(job, category=HazardCategory.SHARED_STATE))


async def test_unknown_category_rejected(session: AsyncSession) -> None:
    _, project = await make_project(session)
    job = new_job(project)
    await add(session, job)
    await assert_violates(session, new_hazard(job, category="STYLE"), "ck_review_hazards_category")


async def test_breaking_hazard_requires_change_type_and_symbol(session: AsyncSession) -> None:
    _, project = await make_project(session)
    job = new_job(project)
    await add(session, job)
    await assert_violates(session, new_hazard(job, is_breaking=True), "breaking_requires_symbol")
    await add(
        session,
        new_hazard(
            job, is_breaking=True, change_type=ChangeType.REMOVED, affected_symbol="users.email"
        ),
    )


# ---------- git_connections / projects ----------


async def test_access_token_connection_requires_token(session: AsyncSession) -> None:
    org = await make_org(session)
    conn = GitConnection(
        org_id=org.id,
        provider=GitProviderKind.GITLAB,
        base_url="https://gitlab.com/api/v4",
        auth_type=GitAuthType.ACCESS_TOKEN,
        account_login="acme",
    )
    await assert_violates(session, conn, "credentials_match_auth_type")


async def test_same_repo_allowed_in_two_orgs(session: AsyncSession) -> None:
    await make_project(session, repo="shared/lib")
    await make_project(session, repo="shared/lib")  # different org + connection -> OK


async def test_duplicate_repo_in_same_connection_rejected(session: AsyncSession) -> None:
    org, project = await make_project(session)
    dup = Project(
        org_id=org.id,
        git_connection_id=project.git_connection_id,
        name="dup",
        repo_identifier=project.repo_identifier,
    )
    await assert_violates(session, dup, "uq_projects_git_connection_id_repo_identifier")


async def test_project_connection_must_belong_to_same_org(session: AsyncSession) -> None:
    org_a = await make_org(session, "A")
    org_b = await make_org(session, "B")
    conn_a = await make_connection(session, org_a)
    project = Project(org_id=org_b.id, git_connection_id=conn_a.id, name="x", repo_identifier="a/x")
    await assert_violates(session, project, "fk_projects_git_connection_id_org_id_git_connections")


# ---------- users / tokens / digests ----------


async def test_user_email_globally_unique(session: AsyncSession) -> None:
    org_a = await make_org(session, "A")
    org_b = await make_org(session, "B")
    await add(session, User(org_id=org_a.id, email="lead@acme.io", display_name="Lead"))
    dup = User(org_id=org_b.id, email="lead@acme.io", display_name="Lead 2")
    await assert_violates(session, dup, "uq_users_email")


async def test_api_token_hash_unique(session: AsyncSession) -> None:
    org = await make_org(session)
    user = User(org_id=org.id, email="dev@acme.io", display_name="Dev")
    await add(session, user)
    issued = generate_api_token()
    fields = {
        "user_id": user.id,
        "token_prefix": issued.display_prefix,
        "token_hash": issued.token_hash,
    }
    await add(session, ApiToken(name="laptop", **fields))
    await assert_violates(session, ApiToken(name="dup", **fields), "uq_api_tokens_token_hash")


async def test_one_digest_per_org_per_day(session: AsyncSession) -> None:
    org = await make_org(session)
    start = datetime(2026, 10, 12, 11, 30, tzinfo=UTC)

    def digest() -> DailyDigest:
        return DailyDigest(
            id=uuid.uuid4(),
            org_id=org.id,
            digest_date=date(2026, 10, 12),
            window_start=start,
            window_end=start + timedelta(days=1),
        )

    await add(session, digest())
    await assert_violates(session, digest(), "uq_daily_digests_org_id_digest_date")


async def test_digest_window_order(session: AsyncSession) -> None:
    org = await make_org(session)
    start = datetime(2026, 10, 12, tzinfo=UTC)
    bad = DailyDigest(
        org_id=org.id, digest_date=date(2026, 10, 12), window_start=start, window_end=start
    )
    await assert_violates(session, bad, "window_order")
