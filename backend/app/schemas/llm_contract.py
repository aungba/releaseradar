"""Structured output contract for every LLM call (PRD §6).

Enums come from ``app.domain.enums`` so they always match the DB CHECK constraints.
"""

from typing import Self

from pydantic import BaseModel, Field, model_validator

from app.domain.enums import ChangeType, HazardCategory, Severity, Verdict

# Names used in the PRD.
SeverityEnum = Severity
CategoryEnum = HazardCategory
VerdictEnum = Verdict
ChangeTypeEnum = ChangeType


class HazardItem(BaseModel):
    category: CategoryEnum
    severity: SeverityEnum
    file_path: str = Field(description="Relative path of the modified file")
    hazard_title: str = Field(max_length=255, description="Concise description of the hazard")
    explanation: str = Field(description="Why this change is dangerous in production")
    remediation: str = Field(description="Step required to safely deploy or resolve")
    is_breaking: bool = Field(
        default=False, description="True if this breaks an existing contract (API, schema, env)"
    )
    change_type: ChangeTypeEnum | None = Field(
        default=None, description="Required when is_breaking is true"
    )
    affected_symbol: str | None = Field(
        default=None,
        max_length=255,
        description="e.g. 'POST /v1/payments', 'users.email', 'STRIPE_KEY'; required when breaking",
    )

    @model_validator(mode="after")
    def _breaking_requires_symbol(self) -> Self:
        if self.is_breaking and (self.change_type is None or not self.affected_symbol):
            raise ValueError("breaking hazards require change_type and affected_symbol")
        return self


class DeploymentPrerequisites(BaseModel):
    required_env_vars: list[str] = Field(default_factory=list)
    pre_deploy_actions: list[str] = Field(default_factory=list)
    zero_downtime_rollback_safe: bool = Field(
        description="True if safe to immediately revert target commit"
    )
    rollback_notes: str = Field(
        description="Why rollback is or is not safe (e.g. irreversible column drop)"
    )


class ReleaseReviewReport(BaseModel):
    verdict: VerdictEnum
    risk_score: int = Field(
        ge=0, le=100, description="Risk index between 0 (safe) and 100 (critical hazard)"
    )
    executive_summary: str = Field(
        description="1-2 sentences summarizing changes and points of failure"
    )
    hazards: list[HazardItem]
    deployment_prerequisites: DeploymentPrerequisites
