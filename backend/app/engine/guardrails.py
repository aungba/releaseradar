"""Stage 7: deterministic post-processing. Guardrails may only make a report *stricter*."""

from collections.abc import Iterable, Sequence

from app.domain.enums import Severity, Verdict
from app.engine.models import GuardrailAction, Signal, SignalKind
from app.schemas.llm_contract import HazardItem, ReleaseReviewReport

VERDICT_BANDS: dict[Verdict, tuple[int, int]] = {
    Verdict.APPROVED: (0, 29),
    Verdict.APPROVED_WITH_CAUTION: (30, 69),
    Verdict.BLOCKED: (70, 100),
}
_SEVERITY_RANK = {Severity.LOW: 0, Severity.MEDIUM: 1, Severity.HIGH: 2, Severity.CRITICAL: 3}
_MAX_ADDED_MIGRATION_STEPS = 5


def _normalize_path(path: str) -> str:
    path = path.strip().strip("`")
    for prefix in ("./", "a/", "b/"):
        if path.startswith(prefix):
            path = path[len(prefix) :]
    return path


def apply(
    report: ReleaseReviewReport,
    *,
    changed_paths: Iterable[str],
    signals: Sequence[Signal],
    partial: bool,
    not_analyzed_count: int = 0,
) -> tuple[ReleaseReviewReport, list[GuardrailAction]]:
    actions: list[GuardrailAction] = []
    paths = set(changed_paths)

    # G1: drop hazards that cite files not in the diff (hallucinations).
    hazards: list[HazardItem] = []
    for hazard in report.hazards:
        path = _normalize_path(hazard.file_path)
        if path not in paths:
            actions.append(
                GuardrailAction(rule="G1", detail=f"dropped hazard for unknown file {path!r}")
            )
            continue
        hazards.append(
            hazard if path == hazard.file_path else hazard.model_copy(update={"file_path": path})
        )

    # G2: merge duplicates (same file + category + symbol), keep the higher severity.
    merged: dict[tuple[str, str, str], HazardItem] = {}
    for hazard in hazards:
        key = (
            hazard.file_path,
            hazard.category,
            (hazard.affected_symbol or hazard.hazard_title).lower(),
        )
        existing = merged.get(key)
        if existing is None:
            merged[key] = hazard
            continue
        actions.append(
            GuardrailAction(rule="G2", detail=f"merged duplicate hazard in {hazard.file_path}")
        )
        if _SEVERITY_RANK[hazard.severity] > _SEVERITY_RANK[existing.severity]:
            merged[key] = hazard
    hazards = list(merged.values())

    verdict = report.verdict
    summary = report.executive_summary

    # G4: a CRITICAL hazard can never be APPROVED.
    if verdict is Verdict.APPROVED and any(h.severity is Severity.CRITICAL for h in hazards):
        verdict = Verdict.APPROVED_WITH_CAUTION
        actions.append(GuardrailAction(rule="G4", detail="CRITICAL hazard present; raised verdict"))

    # G8: partial coverage can never be APPROVED.
    if partial:
        if verdict is Verdict.APPROVED:
            verdict = Verdict.APPROVED_WITH_CAUTION
            actions.append(GuardrailAction(rule="G8", detail="partial coverage; raised verdict"))
        note = (
            f" Note: this analysis is partial — {not_analyzed_count} changed file(s)"
            " could not be analyzed."
            if not_analyzed_count
            else " Note: this analysis is partial — the git provider did not return every change."
        )
        if "analysis is partial" not in summary:
            summary = summary.rstrip() + note

    # G3: clamp the score into the verdict's band (the verdict wins).
    low, high = VERDICT_BANDS[verdict]
    score = min(max(report.risk_score, low), high)
    if score != report.risk_score:
        actions.append(
            GuardrailAction(
                rule="G3", detail=f"risk score {report.risk_score} → {score} for {verdict}"
            )
        )

    prereqs = report.deployment_prerequisites
    env_vars = list(prereqs.required_env_vars)
    actions_list = list(prereqs.pre_deploy_actions)
    rollback_safe = prereqs.zero_downtime_rollback_safe
    rollback_notes = prereqs.rollback_notes

    # G5: detected new env vars must be on the checklist.
    known = {v.strip().upper() for v in env_vars}
    for signal in signals:
        if (
            signal.kind is SignalKind.ENV_VAR_ADDED
            and signal.symbol
            and signal.symbol.upper() not in known
        ):
            env_vars.append(signal.symbol)
            known.add(signal.symbol.upper())
            actions.append(
                GuardrailAction(rule="G5", detail=f"added required env var {signal.symbol}")
            )

    # G6: a destructive schema change makes rollback unsafe.
    destructive = [s for s in signals if s.kind is SignalKind.DESTRUCTIVE_SQL]
    if destructive and rollback_safe:
        rollback_safe = False
        first = destructive[0]
        rollback_notes = (
            rollback_notes.rstrip()
            + f" ReleaseRadar detected a destructive schema change in `{first.file_path}`"
            + f" (`{first.evidence}`); reverting the code will not restore"
            " the dropped or altered data."
        ).strip()
        actions.append(
            GuardrailAction(rule="G6", detail=f"rollback_safe forced false ({first.file_path})")
        )

    # G7: new migrations need a pre-deploy step.
    migrations = [s for s in signals if s.kind is SignalKind.MIGRATION_ADDED]
    if migrations and not any("migrat" in a.lower() for a in actions_list):
        for signal in migrations[:_MAX_ADDED_MIGRATION_STEPS]:
            actions_list.append(
                f"Run database migration `{signal.file_path}` before deploying the new code"
            )
        actions.append(
            GuardrailAction(
                rule="G7",
                detail=f"added {len(migrations[:_MAX_ADDED_MIGRATION_STEPS])} migration step(s)",
            )
        )

    # G9: CRITICAL → LOW, breaking first.
    hazards.sort(key=lambda h: (-_SEVERITY_RANK[h.severity], not h.is_breaking))

    result = report.model_copy(
        update={
            "verdict": verdict,
            "risk_score": score,
            "executive_summary": summary,
            "hazards": hazards,
            "deployment_prerequisites": prereqs.model_copy(
                update={
                    "required_env_vars": env_vars,
                    "pre_deploy_actions": actions_list,
                    "zero_downtime_rollback_safe": rollback_safe,
                    "rollback_notes": rollback_notes,
                }
            ),
        }
    )
    return result, actions
