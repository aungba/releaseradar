"""Eval scoring: hazard matching, false positives, aggregation and targets."""

from typing import Any

from app.domain.enums import Verdict
from app.evals.scoring import (
    AggregateScore,
    Expected,
    ExpectedHazard,
    aggregate,
    failed_targets,
    matches,
    score_case,
)
from app.schemas.llm_contract import HazardItem, ReleaseReviewReport
from tests.engine.helpers import hazard, report_json


def _item(file_path: str, **kwargs: Any) -> HazardItem:
    return HazardItem.model_validate(hazard(file_path, **kwargs))


def _want(**kwargs: Any) -> ExpectedHazard:
    return ExpectedHazard.model_validate(kwargs)


def _report(**kwargs: Any) -> ReleaseReviewReport:
    return ReleaseReviewReport.model_validate_json(report_json(**kwargs))


def test_category_accepts_one_or_many() -> None:
    item = _item("app/api/users.py", category="DATABASE")
    assert matches(item, _want(category="DATABASE", file="app/api/*.py"))
    assert matches(item, _want(category=["API_CONTRACT", "DATABASE"], file="**/users.py"))
    assert not matches(item, _want(category="SECURITY", file="**/users.py"))


def test_file_glob_must_match() -> None:
    item = _item("app/api/users.py")
    assert not matches(item, _want(category="API_CONTRACT", file="app/services/*.py"))


def test_min_severity_and_breaking() -> None:
    medium = _item("a.py", severity="MEDIUM")
    assert matches(medium, _want(category="API_CONTRACT", file="a.py", min_severity="MEDIUM"))
    assert not matches(medium, _want(category="API_CONTRACT", file="a.py", min_severity="HIGH"))
    assert not matches(medium, _want(category="API_CONTRACT", file="a.py", breaking=True))
    breaking = _item("a.py", breaking=True, symbol="GET /v1/users")
    assert matches(breaking, _want(category="API_CONTRACT", file="a.py", breaking=True))


def test_symbol_is_fuzzy_over_symbol_and_title() -> None:
    by_symbol = _item("a.py", breaking=True, symbol="GET /v1/reports/export")
    assert matches(
        by_symbol, _want(category="API_CONTRACT", file="a.py", symbol="/v1/reports/export")
    )
    by_title = _item("a.py", title="Drops users.legacy_email column")
    assert matches(by_title, _want(category="API_CONTRACT", file="a.py", symbol="legacy_email"))
    assert not matches(by_title, _want(category="API_CONTRACT", file="a.py", symbol="billing_id"))


def test_score_case_counts_misses_and_false_positives() -> None:
    expected = Expected.model_validate(
        {
            "verdict": "BLOCKED",
            "rollback_safe": False,
            "required_env_vars": ["STRIPE_WEBHOOK_SECRET", "OTHER"],
            "hazards": [
                {"category": "DATABASE", "file": "migrations/*.sql"},  # tier 1
                {"category": "API_CONTRACT", "file": "app/services/x.py"},
            ],
            "allowed_extra": [{"category": "DATABASE", "file": "app/models/*.py"}],
        }
    )
    report = _report(
        verdict="BLOCKED",
        risk_score=80,
        rollback_safe=True,
        env_vars=[" stripe_webhook_secret "],
        hazards=[
            hazard("migrations/0042.sql", category="DATABASE"),
            hazard("app/models/user.py", category="DATABASE"),  # allowed extra
            hazard("app/utils.py", category="SHARED_STATE"),  # false positive
        ],
    )
    score = score_case("c", report, expected)
    assert (score.matched_hazards, score.expected_hazards) == (1, 2)
    assert (score.matched_tier1, score.expected_tier1) == (1, 1)
    assert score.false_positives == 1
    assert score.missed == ["API_CONTRACT app/services/x.py"]
    assert score.verdict_ok is True
    assert score.rollback_ok is False
    assert (score.env_found, score.env_expected) == (1, 2)


def test_one_prediction_matches_at_most_one_expectation() -> None:
    expected = Expected.model_validate({"hazards": [{"category": "DATABASE", "file": "*.sql"}] * 2})
    report = _report(hazards=[hazard("a.sql", category="DATABASE")])
    assert score_case("c", report, expected).matched_hazards == 1


def test_unspecified_expectations_are_not_scored() -> None:
    score = score_case("c", _report(), Expected())
    assert score.verdict_ok is None and score.rollback_ok is None
    assert score.recall == 1.0


def test_aggregate_and_failed_targets() -> None:
    ok = score_case(
        "a",
        _report(verdict="APPROVED", risk_score=10, rollback_safe=True),
        Expected(verdict=[Verdict.APPROVED], rollback_safe=True),
    )
    bad = score_case(
        "b",
        _report(hazards=[hazard("x.py"), hazard("y.py")]),
        Expected.model_validate(
            {"rollback_safe": False, "hazards": [{"category": "DATABASE", "file": "migrations/*"}]}
        ),
    )
    agg = aggregate([ok, bad])
    assert agg.cases == 2
    assert agg.tier1_recall == 0.0
    assert agg.rollback_accuracy == 0.5
    assert agg.false_positives_per_case == 1.0
    failures = failed_targets(agg)
    assert any(f.startswith("tier1_recall") for f in failures)
    assert any(f.startswith("rollback_accuracy") for f in failures)
    assert not any(f.startswith("false_positives") for f in failures)  # 1.0 is allowed


def test_perfect_score_has_no_failures() -> None:
    agg = AggregateScore(
        cases=1, tier1_recall=1, recall=1, false_positives_per_case=0,
        verdict_accuracy=1, rollback_accuracy=1, env_recall=1,
    )  # fmt: skip
    assert failed_targets(agg) == []
    assert aggregate([]).cases == 0
