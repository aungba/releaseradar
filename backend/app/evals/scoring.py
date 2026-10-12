"""Eval scoring: compare a report with a case's expected findings (pure, unit-tested)."""

import re
from collections.abc import Sequence

from pydantic import BaseModel, Field, field_validator

from app.domain.enums import HazardCategory, Severity, Verdict
from app.engine.globs import glob_match
from app.engine.models import Tier
from app.engine.tiering import path_tier
from app.schemas.llm_contract import HazardItem, ReleaseReviewReport

_SEVERITY_RANK = {Severity.LOW: 0, Severity.MEDIUM: 1, Severity.HIGH: 2, Severity.CRITICAL: 3}


class ExpectedHazard(BaseModel):
    category: list[HazardCategory]  # any of these is acceptable
    file: str  # glob
    min_severity: Severity | None = None
    breaking: bool | None = None
    symbol: str | None = None  # fuzzy: either string contains the other, ignoring punctuation

    @field_validator("category", mode="before")
    @classmethod
    def _one_or_many(cls, value: object) -> object:
        return [value] if isinstance(value, str) else value

    @property
    def is_tier1(self) -> bool:
        return path_tier(self.file.replace("**/", "").replace("*", "x")) is Tier.CRITICAL


class Expected(BaseModel):
    verdict: list[Verdict] = Field(default_factory=list)  # acceptable verdicts; empty = any
    rollback_safe: bool | None = None
    required_env_vars: list[str] = Field(default_factory=list)
    hazards: list[ExpectedHazard] = Field(default_factory=list)
    allowed_extra: list[ExpectedHazard] = Field(default_factory=list)

    @field_validator("verdict", mode="before")
    @classmethod
    def _one_or_many(cls, value: object) -> object:
        return [value] if isinstance(value, str) else value


class CaseScore(BaseModel):
    case_id: str
    expected_hazards: int
    matched_hazards: int
    expected_tier1: int
    matched_tier1: int
    false_positives: int
    missed: list[str]
    unexpected: list[str]
    verdict_ok: bool | None
    rollback_ok: bool | None
    env_expected: int
    env_found: int

    @property
    def recall(self) -> float:
        return self.matched_hazards / self.expected_hazards if self.expected_hazards else 1.0


class AggregateScore(BaseModel):
    cases: int
    tier1_recall: float
    recall: float
    false_positives_per_case: float
    verdict_accuracy: float
    rollback_accuracy: float
    env_recall: float


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9/]", "", text.lower())


def matches(predicted: HazardItem, expected: ExpectedHazard) -> bool:
    if predicted.category not in expected.category:
        return False
    if not glob_match(predicted.file_path, expected.file):
        return False
    if expected.min_severity is not None and (
        _SEVERITY_RANK[predicted.severity] < _SEVERITY_RANK[expected.min_severity]
    ):
        return False
    if expected.breaking is not None and predicted.is_breaking is not expected.breaking:
        return False
    if expected.symbol:
        want = _norm(expected.symbol)
        got = _norm(" ".join(filter(None, [predicted.affected_symbol, predicted.hazard_title])))
        if want not in got and not (
            predicted.affected_symbol and _norm(predicted.affected_symbol) in want
        ):
            return False
    return True


def _describe(item: ExpectedHazard | HazardItem) -> str:
    if isinstance(item, ExpectedHazard):
        return f"{'/'.join(c.value for c in item.category)} {item.file} {item.symbol or ''}".strip()
    return f"{item.category.value} {item.file_path}: {item.hazard_title}"


def score_case(case_id: str, report: ReleaseReviewReport, expected: Expected) -> CaseScore:
    unmatched = list(report.hazards)
    matched = matched_tier1 = 0
    missed: list[str] = []
    for want in expected.hazards:
        hit = next((h for h in unmatched if matches(h, want)), None)
        if hit is None:
            missed.append(_describe(want))
            continue
        unmatched.remove(hit)
        matched += 1
        matched_tier1 += want.is_tier1
    unexpected = [h for h in unmatched if not any(matches(h, a) for a in expected.allowed_extra)]

    found_env = {v.strip().upper() for v in report.deployment_prerequisites.required_env_vars}
    want_env = {v.upper() for v in expected.required_env_vars}
    rollback = report.deployment_prerequisites.zero_downtime_rollback_safe
    return CaseScore(
        case_id=case_id,
        expected_hazards=len(expected.hazards),
        matched_hazards=matched,
        expected_tier1=sum(h.is_tier1 for h in expected.hazards),
        matched_tier1=matched_tier1,
        false_positives=len(unexpected),
        missed=missed,
        unexpected=[_describe(h) for h in unexpected],
        verdict_ok=report.verdict in expected.verdict if expected.verdict else None,
        rollback_ok=rollback is expected.rollback_safe
        if expected.rollback_safe is not None
        else None,
        env_expected=len(want_env),
        env_found=len(want_env & found_env),
    )


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0


def aggregate(scores: Sequence[CaseScore]) -> AggregateScore:
    verdicts = [s.verdict_ok for s in scores if s.verdict_ok is not None]
    rollbacks = [s.rollback_ok for s in scores if s.rollback_ok is not None]
    return AggregateScore(
        cases=len(scores),
        tier1_recall=_ratio(
            sum(s.matched_tier1 for s in scores), sum(s.expected_tier1 for s in scores)
        ),
        recall=_ratio(
            sum(s.matched_hazards for s in scores), sum(s.expected_hazards for s in scores)
        ),
        false_positives_per_case=sum(s.false_positives for s in scores) / len(scores)
        if scores
        else 0.0,
        verdict_accuracy=_ratio(sum(verdicts), len(verdicts)),
        rollback_accuracy=_ratio(sum(rollbacks), len(rollbacks)),
        env_recall=_ratio(sum(s.env_found for s in scores), sum(s.env_expected for s in scores)),
    )


# Starting targets from the M2 plan §1; tune after the first baseline.
TARGETS = {
    "tier1_recall": 0.85,
    "rollback_accuracy": 0.90,
    "env_recall": 0.95,
}
MAX_FALSE_POSITIVES_PER_CASE = 1.0


def failed_targets(score: AggregateScore) -> list[str]:
    failures = [
        f"{name} {getattr(score, name):.2f} < {target:.2f}"
        for name, target in TARGETS.items()
        if getattr(score, name) < target
    ]
    if score.false_positives_per_case > MAX_FALSE_POSITIVES_PER_CASE:
        failures.append(
            f"false_positives_per_case {score.false_positives_per_case:.2f}"
            f" > {MAX_FALSE_POSITIVES_PER_CASE}"
        )
    return failures
