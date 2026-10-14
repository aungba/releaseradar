"""Pydantic request and response DTOs for the /api/v1 HTTP surface (M3)."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.enums import (
    AuditEventType,
    ChangeType,
    GitAuthType,
    GitProviderKind,
    HazardCategory,
    JobStatus,
    PrerequisiteKind,
    ReviewType,
    RiskRating,
    Severity,
    UserRole,
    Verdict,
)
from app.integrations.git.base import RefKind
from app.services.git_connections import TokenHealth


class _Schema(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------- Auth & Tokens


class SessionLoginRequest(BaseModel):
    token: str = Field(..., min_length=4, description="ReleaseRadar access token (rr_...)")


class OrganizationResponse(_Schema):
    id: uuid.UUID
    name: str
    timezone: str


class UserResponse(_Schema):
    id: uuid.UUID
    org_id: uuid.UUID
    email: str
    display_name: str
    role: UserRole


class CurrentUserResponse(BaseModel):
    user: UserResponse
    organization: OrganizationResponse
    current_token_id: uuid.UUID
    current_token_expires_at: datetime | None


class CreateApiTokenRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    expires_in_days: int | None = Field(default=90, ge=1, le=3650)


class ApiTokenResponse(_Schema):
    id: uuid.UUID
    name: str
    token_prefix: str
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class CreatedApiTokenResponse(ApiTokenResponse):
    plaintext: str = Field(..., description="Shown once on creation; never stored in plaintext.")


# ---------------------------------------------------------------- Git Connections


class CreateGitConnectionRequest(BaseModel):
    provider: GitProviderKind
    base_url: str | None = Field(
        default=None, max_length=255, description="Defaults to official cloud API URL if omitted."
    )
    account_login: str = Field(..., min_length=1, max_length=255)
    access_token: str = Field(..., min_length=1)
    token_expires_at: datetime | None = None


class UpdateGitConnectionRequest(BaseModel):
    access_token: str | None = Field(default=None, min_length=1)
    token_expires_at: datetime | None = None


class GitConnectionResponse(_Schema):
    id: uuid.UUID
    org_id: uuid.UUID
    provider: GitProviderKind
    base_url: str
    auth_type: GitAuthType
    account_login: str
    token_expires_at: datetime | None
    last_verified_at: datetime | None
    token_health: TokenHealth
    created_at: datetime


# ---------------------------------------------------------------- Projects & Refs


class CreateProjectRequest(BaseModel):
    git_connection_id: uuid.UUID
    repo_identifier: str = Field(..., min_length=1, max_length=255)
    name: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("repo_identifier")
    @classmethod
    def _strip_repo(cls, value: str) -> str:
        cleaned = value.strip().strip("/")
        if not cleaned:
            raise ValueError("repo_identifier must not be empty")
        return cleaned


class ProjectResponse(_Schema):
    id: uuid.UUID
    org_id: uuid.UUID
    git_connection_id: uuid.UUID
    name: str
    repo_identifier: str
    external_repo_id: str | None
    default_branch: str
    is_active: bool
    provider: GitProviderKind | None = None
    token_health: TokenHealth | None = None
    created_at: datetime


class RefResponse(BaseModel):
    name: str
    kind: RefKind
    sha: str | None = None


# ---------------------------------------------------------------- Reviews & Sign-Off


class CreateReviewRequest(BaseModel):
    project_id: uuid.UUID
    base_ref: str = Field(..., min_length=1, max_length=255)
    target_ref: str = Field(..., min_length=1, max_length=255)

    @field_validator("base_ref", "target_ref")
    @classmethod
    def _strip_ref(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("ref must not be empty")
        return cleaned


class HazardResponse(_Schema):
    id: uuid.UUID
    review_job_id: uuid.UUID
    category: HazardCategory
    severity: Severity
    file_path: str
    hazard_title: str
    explanation: str
    remediation: str
    is_breaking: bool
    change_type: ChangeType | None
    affected_symbol: str | None
    acknowledged_by: uuid.UUID | None
    acknowledged_at: datetime | None


class PrerequisiteResponse(_Schema):
    id: uuid.UUID
    review_job_id: uuid.UUID
    kind: PrerequisiteKind
    description: str
    sort_order: int
    completed_by: uuid.UUID | None
    completed_at: datetime | None


class SignoffSummary(BaseModel):
    is_signed_off: bool
    signed_off_by: str | None = None
    signed_off_at: datetime | None = None
    signoff_note: str | None = None
    can_sign_off: bool
    blocking_reasons: list[str] = Field(default_factory=list)


class ReviewJobSummary(_Schema):
    id: uuid.UUID
    project_id: uuid.UUID
    triggered_by: uuid.UUID | None
    review_type: ReviewType
    status: JobStatus
    base_ref: str
    target_ref: str
    base_sha: str | None
    target_sha: str | None
    pr_number: int | None
    verdict: Verdict | None
    risk_rating: RiskRating | None = None
    risk_score: int | None
    executive_summary: str | None
    rollback_safe: bool | None
    rollback_notes: str | None
    llm_model: str | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class ReviewDetailResponse(ReviewJobSummary):
    raw_metrics: dict[str, Any]
    hazards: list[HazardResponse]
    prerequisites: list[PrerequisiteResponse]
    signoff_status: SignoffSummary


class NoteRequest(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


class AuditEventResponse(_Schema):
    id: uuid.UUID
    review_job_id: uuid.UUID
    actor_id: uuid.UUID | None
    actor_display: str
    event_type: AuditEventType
    hazard_id: uuid.UUID | None
    prerequisite_id: uuid.UUID | None
    note: str | None
    created_at: datetime
