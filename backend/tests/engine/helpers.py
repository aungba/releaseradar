"""Builders shared by engine tests."""

import json
from typing import Any

from app.domain.enums import ChangeType
from app.integrations.git.base import CompareResult, FileDiff
from app.integrations.git.diffutil import count_changes


def fd(
    path: str,
    patch: str | None = "@@ -1 +1 @@\n-a\n+b\n",
    status: ChangeType = ChangeType.MODIFIED,
    **kwargs: Any,
) -> FileDiff:
    additions, deletions = count_changes(patch)
    values: dict[str, Any] = {"additions": additions, "deletions": deletions, **kwargs}
    return FileDiff(path=path, status=status, patch=patch, **values)


def added(*lines: str) -> str:
    """A hunk that adds the given lines."""
    return f"@@ -0,0 +1,{len(lines)} @@\n" + "".join(f"+{line}\n" for line in lines)


def removed(*lines: str) -> str:
    return f"@@ -1,{len(lines)} +0,0 @@\n" + "".join(f"-{line}\n" for line in lines)


def compare(*files: FileDiff, truncated: bool = False) -> CompareResult:
    return CompareResult(
        base_sha="a" * 40, head_sha="b" * 40, commits=[], files=list(files), truncated=truncated
    )


def report_json(
    *,
    verdict: str = "APPROVED_WITH_CAUTION",
    risk_score: int = 45,
    hazards: list[dict[str, Any]] | None = None,
    env_vars: list[str] | None = None,
    actions: list[str] | None = None,
    rollback_safe: bool = True,
    summary: str = "Adds a payments endpoint.",
) -> str:
    return json.dumps(
        {
            "verdict": verdict,
            "risk_score": risk_score,
            "executive_summary": summary,
            "hazards": hazards or [],
            "deployment_prerequisites": {
                "required_env_vars": env_vars or [],
                "pre_deploy_actions": actions or [],
                "zero_downtime_rollback_safe": rollback_safe,
                "rollback_notes": "Code-only change.",
            },
        }
    )


def hazard(
    file_path: str,
    *,
    category: str = "API_CONTRACT",
    severity: str = "HIGH",
    title: str = "Response field renamed",
    breaking: bool = False,
    symbol: str | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "category": category,
        "severity": severity,
        "file_path": file_path,
        "hazard_title": title,
        "explanation": "Existing clients read the old field.",
        "remediation": "Ship both fields for one release.",
        "is_breaking": breaking,
    }
    if breaking:
        item |= {"change_type": "RENAMED", "affected_symbol": symbol or "GET /v1/x"}
    return item
