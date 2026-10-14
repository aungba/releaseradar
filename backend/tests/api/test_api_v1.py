"""HTTP integration tests for /api/v1 (M3): auth, connections, projects, reviews, sign-off."""

import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.v1.git_connections import get_provider_factory
from app.api.v1.reviews import get_review_enqueuer
from app.core.config import Settings, get_settings
from app.core.crypto import generate_api_token
from app.core.db import get_session
from app.domain.enums import (
    AuditEventType,
    JobStatus,
    UserRole,
)
from app.engine.llm import FakeLLMClient
from app.engine.pipeline import EngineSettings
from app.integrations.git import (
    AuthenticationError,
    GitHubProvider,
    GitProvider,
    Identity,
    Ref,
    RefKind,
    RepositoryInfo,
)
from app.main import create_app
from app.models import ApiToken, GitConnection, Organization, User
from app.services.reviews import run_review
from tests.engine.helpers import hazard, report_json
from tests.integrations import fakes

pytestmark = pytest.mark.db

TEST_FERNET_KEY = Fernet.generate_key().decode()
ENGINE_SETTINGS = EngineSettings(primary_model="gemini/test", fallback_model="groq/test")


class FakeProvider:
    """In-memory GitProvider for API tests."""

    def __init__(
        self,
        *,
        auth_error: bool = False,
        token_expires_at: datetime | None = None,
    ) -> None:
        self.auth_error = auth_error
        self.token_expires_at = token_expires_at

    async def __aenter__(self) -> "FakeProvider":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def verify_credentials(self) -> Identity:
        if self.auth_error:
            raise AuthenticationError("401 Bad credentials")
        return Identity(
            username="bot",
            display_name="Bot",
            token_expires_at=self.token_expires_at,
        )

    async def get_repository(self, repo: str) -> RepositoryInfo:
        if self.auth_error:
            raise AuthenticationError("401 Bad credentials")
        return RepositoryInfo(external_id="repo-123", full_name=repo, default_branch="main")

    async def resolve_ref(self, repo: str, ref: str) -> str:
        return fakes.BASE_SHA if ref == "v1.0.0" else fakes.HEAD_SHA

    async def list_refs(self, repo: str, query: str = "", limit: int = 20) -> list[Ref]:
        all_refs = [
            Ref(name="main", kind=RefKind.BRANCH, sha="1" * 40),
            Ref(name="v1.0.0", kind=RefKind.TAG, sha=fakes.BASE_SHA),
            Ref(name="v1.1.0", kind=RefKind.TAG, sha=fakes.HEAD_SHA),
        ]
        filtered = [r for r in all_refs if r.name.startswith(query)]
        return filtered[:limit]


@pytest.fixture
async def sessionmaker(migrated_database: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    async with engine.connect() as conn:
        trans = await conn.begin()
        yield async_sessionmaker(
            bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False
        )
        await trans.rollback()
    await engine.dispose()


@pytest.fixture
def settings_override(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("RR_ENCRYPTION_KEY", TEST_FERNET_KEY)
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
async def seeded_identities(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> dict[str, str | uuid.UUID]:
    """Create Org A (admin + member) and Org B (admin) with tokens."""
    async with sessionmaker() as s, s.begin():
        org_a = Organization(name="Acme Corp")
        org_b = Organization(name="Beta Inc")
        s.add_all([org_a, org_b])
        await s.flush()

        admin_a = User(
            org_id=org_a.id, email="admin@acme.io", display_name="Alice Admin", role=UserRole.ADMIN
        )
        member_a = User(
            org_id=org_a.id, email="dev@acme.io", display_name="Bob Member", role=UserRole.MEMBER
        )
        admin_b = User(
            org_id=org_b.id, email="admin@beta.io", display_name="Carol Beta", role=UserRole.ADMIN
        )
        s.add_all([admin_a, member_a, admin_b])
        await s.flush()

        tok_admin_a = generate_api_token()
        tok_member_a = generate_api_token()
        tok_admin_b = generate_api_token()
        tok_expired = generate_api_token()
        tok_revoked = generate_api_token()

        now = datetime.now(UTC)
        s.add_all(
            [
                ApiToken(
                    user_id=admin_a.id,
                    name="admin-a",
                    token_prefix=tok_admin_a.display_prefix,
                    token_hash=tok_admin_a.token_hash,
                    expires_at=now + timedelta(days=90),
                ),
                ApiToken(
                    user_id=member_a.id,
                    name="member-a",
                    token_prefix=tok_member_a.display_prefix,
                    token_hash=tok_member_a.token_hash,
                    expires_at=now + timedelta(days=90),
                ),
                ApiToken(
                    user_id=admin_b.id,
                    name="admin-b",
                    token_prefix=tok_admin_b.display_prefix,
                    token_hash=tok_admin_b.token_hash,
                    expires_at=now + timedelta(days=90),
                ),
                ApiToken(
                    user_id=admin_a.id,
                    name="expired",
                    token_prefix=tok_expired.display_prefix,
                    token_hash=tok_expired.token_hash,
                    expires_at=now - timedelta(days=1),
                ),
                ApiToken(
                    user_id=admin_a.id,
                    name="revoked",
                    token_prefix=tok_revoked.display_prefix,
                    token_hash=tok_revoked.token_hash,
                    revoked_at=now - timedelta(hours=1),
                ),
            ]
        )
    return {
        "org_a_id": org_a.id,
        "org_b_id": org_b.id,
        "admin_a_id": admin_a.id,
        "member_a_id": member_a.id,
        "admin_a_token": tok_admin_a.plaintext,
        "member_a_token": tok_member_a.plaintext,
        "admin_b_token": tok_admin_b.plaintext,
        "expired_token": tok_expired.plaintext,
        "revoked_token": tok_revoked.plaintext,
    }


@pytest.fixture
async def api_client(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings_override: Settings,
) -> AsyncIterator[tuple[AsyncClient, list[uuid.UUID], dict[str, bool]]]:
    app = create_app()
    enqueued_jobs: list[uuid.UUID] = []
    provider_state: dict[str, bool] = {"auth_error": False}

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as s:
            yield s

    def override_provider_factory() -> Callable[[GitConnection], GitProvider]:
        def _factory(_conn: GitConnection) -> GitProvider:
            return FakeProvider(auth_error=provider_state["auth_error"])  # type: ignore[return-value]

        return _factory

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_settings] = lambda: settings_override
    app.dependency_overrides[get_provider_factory] = override_provider_factory
    app.dependency_overrides[get_review_enqueuer] = lambda: enqueued_jobs.append

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, enqueued_jobs, provider_state


# ---------------------------------------------------------------- M3.1 Auth & Tokens


async def test_bearer_and_session_cookie_auth(
    api_client: tuple[AsyncClient, list[uuid.UUID], dict[str, bool]],
    seeded_identities: dict[str, str | uuid.UUID],
) -> None:
    client, _, _ = api_client
    token = str(seeded_identities["admin_a_token"])

    # Unauthenticated -> 401
    r = await client.get("/api/v1/me")
    assert r.status_code == 401

    # Expired / revoked / malformed -> 401
    for bad in (
        str(seeded_identities["expired_token"]),
        str(seeded_identities["revoked_token"]),
        "not-a-valid-token",
    ):
        r = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {bad}"})
        assert r.status_code == 401

    # Valid Bearer token -> 200
    r = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["user"]["email"] == "admin@acme.io"
    assert r.json()["organization"]["name"] == "Acme Corp"

    # Exchange token for HttpOnly session cookie
    r = await client.post("/api/v1/auth/session", json={"token": token})
    assert r.status_code == 200
    assert "rr_session" in r.cookies

    # Subsequent request authenticates via cookie without Authorization header
    r = await client.get("/api/v1/me")
    assert r.status_code == 200
    assert r.json()["user"]["email"] == "admin@acme.io"

    # Logout clears cookie
    r = await client.delete("/api/v1/auth/session")
    assert r.status_code == 204
    r = await client.get("/api/v1/me")
    assert r.status_code == 401


async def test_self_service_token_lifecycle_revokes_session(
    api_client: tuple[AsyncClient, list[uuid.UUID], dict[str, bool]],
    seeded_identities: dict[str, str | uuid.UUID],
) -> None:
    client, _, _ = api_client
    admin_headers = {"Authorization": f"Bearer {seeded_identities['admin_a_token']}"}

    # Create a new token
    r = await client.post(
        "/api/v1/me/tokens",
        headers=admin_headers,
        json={"name": "laptop", "expires_in_days": 30},
    )
    assert r.status_code == 201
    created = r.json()
    new_plaintext = created["plaintext"]
    new_id = created["id"]
    assert new_plaintext.startswith("rr_")

    # List tokens (plaintext must not be in list output)
    r = await client.get("/api/v1/me/tokens", headers=admin_headers)
    assert r.status_code == 200
    assert any(t["id"] == new_id and "plaintext" not in t for t in r.json())

    # Log in via session cookie with the new token
    r = await client.post("/api/v1/auth/session", json={"token": new_plaintext})
    assert r.status_code == 200
    assert (await client.get("/api/v1/me")).status_code == 200

    # Revoke the token -> active cookie session immediately fails with 401
    r = await client.delete(f"/api/v1/me/tokens/{new_id}", headers=admin_headers)
    assert r.status_code == 204
    assert (await client.get("/api/v1/me")).status_code == 401


# ---------------------------------------------------------------- M3.2 Connections & Projects


async def test_git_connections_and_projects_onboarding_and_isolation(
    api_client: tuple[AsyncClient, list[uuid.UUID], dict[str, bool]],
    seeded_identities: dict[str, str | uuid.UUID],
) -> None:
    client, _, provider_state = api_client
    admin_a = {"Authorization": f"Bearer {seeded_identities['admin_a_token']}"}
    member_a = {"Authorization": f"Bearer {seeded_identities['member_a_token']}"}
    admin_b = {"Authorization": f"Bearer {seeded_identities['admin_b_token']}"}

    # Member cannot create a git connection -> 403
    r = await client.post(
        "/api/v1/git-connections",
        headers=member_a,
        json={"provider": "github", "account_login": "acme", "access_token": "tok"},
    )
    assert r.status_code == 403

    # Invalid provider token during create -> 422
    provider_state["auth_error"] = True
    r = await client.post(
        "/api/v1/git-connections",
        headers=admin_a,
        json={"provider": "github", "account_login": "acme", "access_token": "bad-tok"},
    )
    assert r.status_code == 422
    provider_state["auth_error"] = False

    # Valid create -> 201, last_verified_at set, encrypted token never exposed
    r = await client.post(
        "/api/v1/git-connections",
        headers=admin_a,
        json={"provider": "github", "account_login": "acme", "access_token": "good-tok"},
    )
    assert r.status_code == 201
    conn_data = r.json()
    conn_id = conn_data["id"]
    assert conn_data["last_verified_at"] is not None
    assert "access_token" not in conn_data and "access_token_encrypted" not in conn_data

    # Org B cannot see or verify Org A's connection -> 404
    assert (await client.get("/api/v1/git-connections", headers=admin_b)).json() == []
    r = await client.post(f"/api/v1/git-connections/{conn_id}/verify", headers=admin_b)
    assert r.status_code == 404

    # Create project -> auto-fills default_branch and external_repo_id
    r = await client.post(
        "/api/v1/projects",
        headers=admin_a,
        json={"git_connection_id": conn_id, "repo_identifier": "acme/payment-service"},
    )
    assert r.status_code == 201
    proj = r.json()
    proj_id = proj["id"]
    assert proj["name"] == "payment-service"
    assert proj["default_branch"] == "main"
    assert proj["external_repo_id"] == "repo-123"

    # Duplicate project on same connection -> 409
    r = await client.post(
        "/api/v1/projects",
        headers=admin_a,
        json={"git_connection_id": conn_id, "repo_identifier": "acme/payment-service"},
    )
    assert r.status_code == 409

    # Ref autocomplete works for Member A, 404 for Org B
    r = await client.get(f"/api/v1/projects/{proj_id}/refs?q=v1", headers=member_a)
    assert r.status_code == 200
    assert [ref["name"] for ref in r.json()] == ["v1.0.0", "v1.1.0"]

    r = await client.get(f"/api/v1/projects/{proj_id}/refs", headers=admin_b)
    assert r.status_code == 404


# ---------------------------------------------------------------- M3.3 & M3.4 Reviews & Sign-Off


async def test_full_review_and_signoff_flow(
    api_client: tuple[AsyncClient, list[uuid.UUID], dict[str, bool]],
    sessionmaker: async_sessionmaker[AsyncSession],
    seeded_identities: dict[str, str | uuid.UUID],
) -> None:
    client, enqueued, _ = api_client
    admin_a = {"Authorization": f"Bearer {seeded_identities['admin_a_token']}"}
    member_a = {"Authorization": f"Bearer {seeded_identities['member_a_token']}"}
    admin_b = {"Authorization": f"Bearer {seeded_identities['admin_b_token']}"}

    # Setup connection + project
    r = await client.post(
        "/api/v1/git-connections",
        headers=admin_a,
        json={"provider": "github", "account_login": "acme", "access_token": "tok"},
    )
    conn_id = r.json()["id"]
    r = await client.post(
        "/api/v1/projects",
        headers=admin_a,
        json={"git_connection_id": conn_id, "repo_identifier": fakes.GITHUB_REPO},
    )
    proj_id = r.json()["id"]

    # Identical base and target refs -> 422
    r = await client.post(
        "/api/v1/reviews",
        headers=member_a,
        json={"project_id": proj_id, "base_ref": "v1.0.0", "target_ref": "v1.0.0"},
    )
    assert r.status_code == 422

    # Trigger review -> 202 Accepted + job enqueued
    r = await client.post(
        "/api/v1/reviews",
        headers=member_a,
        json={"project_id": proj_id, "base_ref": "v1.0.0", "target_ref": "v1.1.0"},
    )
    assert r.status_code == 202
    job_summary = r.json()
    review_id = uuid.UUID(job_summary["id"])
    assert job_summary["status"] == "QUEUED"
    assert enqueued == [review_id]

    # Cannot sign off while QUEUED -> 409
    r = await client.post(f"/api/v1/reviews/{review_id}/signoff", headers=admin_a)
    assert r.status_code == 409

    # Run the worker service against our FakeLLMClient (returns BLOCKED with 1 HIGH + 1 LOW hazard + 1 env var)
    repo_path = f"/api/v3/repos/{fakes.GITHUB_REPO}"
    routes: dict[str, fakes.Handler] = {
        f"{repo_path}/commits/v1.0.0": lambda _r: httpx.Response(200, text=fakes.BASE_SHA),
        f"{repo_path}/commits/v1.1.0": lambda _r: httpx.Response(200, text=fakes.HEAD_SHA),
    }
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(fakes.github_handler(fakes.Recorder(), extra=routes))
    )
    llm = FakeLLMClient(
        [
            report_json(
                verdict="BLOCKED",
                risk_score=85,
                hazards=[
                    hazard("src/app.py", severity="LOW", title="Minor style note"),
                    hazard(
                        "src/app.py",
                        severity="HIGH",
                        title="Endpoint removed",
                        breaking=True,
                        symbol="DELETE /v1/legacy",
                    ),
                ],
                env_vars=["STRIPE_WEBHOOK_SECRET"],
                rollback_safe=False,
            )
        ]
    )
    try:
        status_out = await run_review(
            review_id,
            sessionmaker=sessionmaker,
            llm=llm,
            engine_settings=ENGINE_SETTINGS,
            provider_factory=lambda _c: GitHubProvider(
                fakes.TOKEN, fakes.GITHUB_API, client=http_client
            ),
        )
        assert status_out == JobStatus.COMPLETED
    finally:
        await http_client.aclose()

    # Fetch full review detail
    r = await client.get(f"/api/v1/reviews/{review_id}", headers=member_a)
    assert r.status_code == 200
    detail = r.json()
    assert detail["status"] == "COMPLETED"
    assert detail["verdict"] == "BLOCKED"
    assert detail["risk_rating"] == "RED"
    # Hazards sorted CRITICAL -> LOW (so HIGH comes before LOW)
    assert [h["severity"] for h in detail["hazards"]] == ["HIGH", "LOW"]
    high_hazard_id = detail["hazards"][0]["id"]
    low_hazard_id = detail["hazards"][1]["id"]
    prereq_id = detail["prerequisites"][0]["id"]

    signoff = detail["signoff_status"]
    assert signoff["can_sign_off"] is False
    assert signoff["is_signed_off"] is False
    assert len(signoff["blocking_reasons"]) == 2

    # Org B cannot access review, hazards, prerequisites, or audit -> 404
    assert (await client.get(f"/api/v1/reviews/{review_id}", headers=admin_b)).status_code == 404
    assert (
        await client.put(f"/api/v1/hazards/{high_hazard_id}/acknowledgement", headers=admin_b)
    ).status_code == 404

    # Acknowledge the HIGH hazard (LOW hazard is advisory and not required)
    r = await client.put(
        f"/api/v1/hazards/{high_hazard_id}/acknowledgement",
        headers=member_a,
        json={"note": "Verified no clients call /v1/legacy"},
    )
    assert r.status_code == 200
    assert r.json()["acknowledged_at"] is not None

    # Still blocked from sign-off because prerequisite is incomplete -> 409
    r = await client.post(
        f"/api/v1/reviews/{review_id}/signoff",
        headers=admin_a,
        json={"note": "Override with enough characters"},
    )
    assert r.status_code == 409

    # Complete prerequisite
    r = await client.put(
        f"/api/v1/prerequisites/{prereq_id}/completion",
        headers=member_a,
        json={"note": "Added STRIPE_WEBHOOK_SECRET in Vault"},
    )
    assert r.status_code == 200
    assert r.json()["completed_at"] is not None

    # Now can_sign_off is True, but verdict == BLOCKED requires an override note >= 10 chars
    r = await client.post(
        f"/api/v1/reviews/{review_id}/signoff",
        headers=admin_a,
        json={"note": "short"},
    )
    assert r.status_code == 409

    # Sign off with proper override note -> 200
    r = await client.post(
        f"/api/v1/reviews/{review_id}/signoff",
        headers=admin_a,
        json={"note": "Approved for staged rollout after Vault update."},
    )
    assert r.status_code == 200
    signed = r.json()
    assert signed["is_signed_off"] is True
    assert signed["can_sign_off"] is True
    assert "Alice Admin" in signed["signed_off_by"]

    # Un-acknowledging the LOW hazard does NOT revoke sign-off
    await client.put(f"/api/v1/hazards/{low_hazard_id}/acknowledgement", headers=member_a)
    await client.delete(f"/api/v1/hazards/{low_hazard_id}/acknowledgement", headers=member_a)
    r = await client.get(f"/api/v1/reviews/{review_id}", headers=member_a)
    assert r.json()["signoff_status"]["is_signed_off"] is True

    # Un-acknowledging the HIGH hazard AUTOMATICALLY revokes sign-off!
    r = await client.delete(
        f"/api/v1/hazards/{high_hazard_id}/acknowledgement",
        headers=member_a,
        params={"note": "Wait, mobile still calls this"},
    )
    assert r.status_code == 200
    r = await client.get(f"/api/v1/reviews/{review_id}", headers=member_a)
    after_unack = r.json()["signoff_status"]
    assert after_unack["is_signed_off"] is False
    assert after_unack["can_sign_off"] is False

    # Check chronological audit trail
    r = await client.get(f"/api/v1/reviews/{review_id}/audit", headers=member_a)
    assert r.status_code == 200
    event_types = [e["event_type"] for e in r.json()]
    assert event_types == [
        AuditEventType.HAZARD_ACKNOWLEDGED,
        AuditEventType.PREREQUISITE_COMPLETED,
        AuditEventType.RELEASE_SIGNED_OFF,
        AuditEventType.HAZARD_ACKNOWLEDGED,
        AuditEventType.HAZARD_UNACKNOWLEDGED,
        AuditEventType.HAZARD_UNACKNOWLEDGED,
        AuditEventType.SIGNOFF_REVOKED,
    ]


async def test_prerequisite_reopen_revokes_signoff_and_manual_revoke(
    api_client: tuple[AsyncClient, list[uuid.UUID], dict[str, bool]],
    sessionmaker: async_sessionmaker[AsyncSession],
    seeded_identities: dict[str, str | uuid.UUID],
) -> None:
    client, _, _ = api_client
    admin_a = {"Authorization": f"Bearer {seeded_identities['admin_a_token']}"}
    member_a = {"Authorization": f"Bearer {seeded_identities['member_a_token']}"}

    # Create connection + project + review
    r = await client.post(
        "/api/v1/git-connections",
        headers=admin_a,
        json={"provider": "github", "account_login": "acme", "access_token": "tok"},
    )
    conn_id = r.json()["id"]
    r = await client.post(
        "/api/v1/projects",
        headers=admin_a,
        json={"git_connection_id": conn_id, "repo_identifier": fakes.GITHUB_REPO},
    )
    proj_id = r.json()["id"]

    r = await client.post(
        "/api/v1/reviews",
        headers=member_a,
        json={"project_id": proj_id, "base_ref": "v1.0.0", "target_ref": "v1.1.0"},
    )
    review_id = uuid.UUID(r.json()["id"])

    repo_path = f"/api/v3/repos/{fakes.GITHUB_REPO}"
    routes: dict[str, fakes.Handler] = {
        f"{repo_path}/commits/v1.0.0": lambda _r: httpx.Response(200, text=fakes.BASE_SHA),
        f"{repo_path}/commits/v1.1.0": lambda _r: httpx.Response(200, text=fakes.HEAD_SHA),
    }
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(fakes.github_handler(fakes.Recorder(), extra=routes))
    )
    llm = FakeLLMClient(
        [
            report_json(
                verdict="APPROVED_WITH_CAUTION",
                risk_score=40,
                hazards=[],
                env_vars=["REDIS_URL"],
                rollback_safe=True,
            )
        ]
    )
    try:
        await run_review(
            review_id,
            sessionmaker=sessionmaker,
            llm=llm,
            engine_settings=ENGINE_SETTINGS,
            provider_factory=lambda _c: GitHubProvider(
                fakes.TOKEN, fakes.GITHUB_API, client=http_client
            ),
        )
    finally:
        await http_client.aclose()

    detail = (await client.get(f"/api/v1/reviews/{review_id}", headers=member_a)).json()
    prereq_id = detail["prerequisites"][0]["id"]

    # Complete prerequisite and sign off (APPROVED_WITH_CAUTION does not require a note)
    await client.put(f"/api/v1/prerequisites/{prereq_id}/completion", headers=member_a)
    r = await client.post(f"/api/v1/reviews/{review_id}/signoff", headers=admin_a)
    assert r.status_code == 200
    assert r.json()["is_signed_off"] is True

    # Reopening prerequisite automatically revokes sign-off
    r = await client.delete(f"/api/v1/prerequisites/{prereq_id}/completion", headers=member_a)
    assert r.status_code == 200
    detail = (await client.get(f"/api/v1/reviews/{review_id}", headers=member_a)).json()
    assert detail["signoff_status"]["is_signed_off"] is False

    # Re-complete, sign off, then manually revoke via DELETE /reviews/{id}/signoff
    await client.put(f"/api/v1/prerequisites/{prereq_id}/completion", headers=member_a)
    await client.post(f"/api/v1/reviews/{review_id}/signoff", headers=admin_a)
    r = await client.delete(
        f"/api/v1/reviews/{review_id}/signoff",
        headers=admin_a,
        params={"note": "Hold deploy until after freeze"},
    )
    assert r.status_code == 200
    assert r.json()["is_signed_off"] is False
    assert r.json()["can_sign_off"] is True

    # List reviews filtering by project_id and status
    r = await client.get(f"/api/v1/reviews?project_id={proj_id}&status=COMPLETED", headers=member_a)
    assert r.status_code == 200
    assert len(r.json()) == 1 and r.json()[0]["id"] == str(review_id)


async def test_cli_create_user_and_create_token_helpers(
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import typer

    from app.cli import _create_token_for_email, _create_user_and_token
    from app.core import db as db_mod

    monkeypatch.setattr(db_mod, "get_sessionmaker", lambda: sessionmaker)
    # Also patch cli's imported get_sessionmaker reference
    import app.cli as cli_mod

    monkeypatch.setattr(cli_mod, "get_sessionmaker", lambda: sessionmaker)

    tok1 = await _create_user_and_token(
        "New Pilot Org", "lead@pilot.io", "Lead", UserRole.ADMIN, 90
    )
    assert tok1.startswith("rr_")

    # Duplicate email raises BadParameter
    with pytest.raises(typer.BadParameter):
        await _create_user_and_token("New Pilot Org", "lead@pilot.io", "Lead", UserRole.ADMIN, 90)

    tok2 = await _create_token_for_email("lead@pilot.io", "ci-pipeline", 30)
    assert tok2.startswith("rr_") and tok2 != tok1
