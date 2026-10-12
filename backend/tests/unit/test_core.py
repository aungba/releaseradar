import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.core.crypto import decrypt_secret, encrypt_secret, generate_api_token, hash_api_token
from app.domain.enums import RiskRating, Verdict, risk_rating_for
from app.main import app
from app.schemas.llm_contract import HazardItem, ReleaseReviewReport


def _report(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "verdict": "APPROVED_WITH_CAUTION",
        "risk_score": 55,
        "executive_summary": "Drops users.legacy_id; requires new env var.",
        "hazards": [
            {
                "category": "DATABASE",
                "severity": "HIGH",
                "file_path": "migrations/0042.sql",
                "hazard_title": "Column dropped",
                "explanation": "Old pods still read it.",
                "remediation": "Two-phase deploy.",
                "is_breaking": True,
                "change_type": "REMOVED",
                "affected_symbol": "users.legacy_id",
            }
        ],
        "deployment_prerequisites": {
            "required_env_vars": ["STRIPE_WEBHOOK_SECRET"],
            "pre_deploy_actions": ["Run migration 0042"],
            "zero_downtime_rollback_safe": False,
            "rollback_notes": "Column drop is irreversible.",
        },
    }
    data.update(overrides)
    return data


# ---------- LLM contract ----------


def test_report_round_trip() -> None:
    report = ReleaseReviewReport.model_validate(_report())
    assert report.verdict is Verdict.APPROVED_WITH_CAUTION
    assert ReleaseReviewReport.model_validate_json(report.model_dump_json()) == report


def test_report_rejects_out_of_range_score() -> None:
    with pytest.raises(ValidationError):
        ReleaseReviewReport.model_validate(_report(risk_score=150))


def test_breaking_hazard_requires_symbol() -> None:
    with pytest.raises(ValidationError, match="affected_symbol"):
        HazardItem.model_validate(
            {
                "category": "API_CONTRACT",
                "severity": "CRITICAL",
                "file_path": "api/routes.py",
                "hazard_title": "Route removed",
                "explanation": "Mobile clients call it.",
                "remediation": "Keep a deprecated alias.",
                "is_breaking": True,
            }
        )


def test_json_schema_includes_shared_state() -> None:
    schema = ReleaseReviewReport.model_json_schema()
    assert "SHARED_STATE" in schema["$defs"]["HazardCategory"]["enum"]


# ---------- domain ----------


@pytest.mark.parametrize(
    ("verdict", "rating"),
    [
        (Verdict.APPROVED, RiskRating.GREEN),
        (Verdict.APPROVED_WITH_CAUTION, RiskRating.AMBER),
        (Verdict.BLOCKED, RiskRating.RED),
    ],
)
def test_risk_rating_mapping(verdict: Verdict, rating: RiskRating) -> None:
    assert risk_rating_for(verdict) is rating


# ---------- crypto ----------


def test_secret_encryption_round_trip() -> None:
    key = Fernet.generate_key().decode()
    ciphertext = encrypt_secret("glpat-secret", key=key)
    assert ciphertext != "glpat-secret"
    assert decrypt_secret(ciphertext, key=key) == "glpat-secret"


def test_missing_encryption_key_fails_loudly() -> None:
    with pytest.raises(RuntimeError, match="RR_ENCRYPTION_KEY"):
        encrypt_secret("x", key="")


def test_api_token_generation() -> None:
    issued = generate_api_token()
    assert issued.plaintext.startswith("rr_")
    assert issued.plaintext.startswith(issued.display_prefix)
    assert len(issued.display_prefix) <= 16
    assert issued.token_hash == hash_api_token(issued.plaintext)
    assert len(issued.token_hash) == 64
    assert generate_api_token().plaintext != issued.plaintext


# ---------- API ----------


async def test_health_endpoint() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
