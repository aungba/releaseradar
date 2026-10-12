"""Engine data types."""

from enum import IntEnum, StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import Verdict
from app.integrations.git.base import Commit, FileDiff
from app.schemas.llm_contract import ReleaseReviewReport


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class Tier(IntEnum):
    CRITICAL = 1  # migrations, schemas, routers, config, manifests, auth
    CORE = 2  # application code
    LOW = 3  # tests, docs, styles


class SkipReason(StrEnum):
    NOISE = "NOISE"  # lockfiles, build output, media, vendored, generated
    BINARY = "BINARY"
    PATCH_OMITTED = "PATCH_OMITTED"  # provider withheld the patch and the fallback could not get it
    BUDGET = "BUDGET"  # did not fit the model's input budget


class Coverage(StrEnum):
    FULL = "FULL"
    PARTIAL = "PARTIAL"  # Tier 1/2 content was not analyzed, or the provider truncated


class SignalKind(StrEnum):
    ENV_VAR_ADDED = "ENV_VAR_ADDED"
    ENV_VAR_REMOVED = "ENV_VAR_REMOVED"
    DESTRUCTIVE_SQL = "DESTRUCTIVE_SQL"
    MIGRATION_ADDED = "MIGRATION_ADDED"
    ROUTE_REMOVED = "ROUTE_REMOVED"
    ROUTE_CHANGED = "ROUTE_CHANGED"
    DEPENDENCY_CHANGED = "DEPENDENCY_CHANGED"


# Signals strong enough to promote a file to Tier 1.
PROMOTING_SIGNALS = frozenset(
    {
        SignalKind.ENV_VAR_ADDED,
        SignalKind.ENV_VAR_REMOVED,
        SignalKind.DESTRUCTIVE_SQL,
        SignalKind.MIGRATION_ADDED,
        SignalKind.ROUTE_REMOVED,
        SignalKind.ROUTE_CHANGED,
    }
)


class Signal(_Frozen):
    """A fact found deterministically in the diff."""

    kind: SignalKind
    file_path: str
    symbol: str | None = None
    evidence: str = ""  # the triggering line, trimmed


class AnalysisFile(_Frozen):
    diff: FileDiff
    tier: Tier
    signals: tuple[Signal, ...] = ()

    @property
    def path(self) -> str:
        return self.diff.path


class SkippedFile(_Frozen):
    path: str
    reason: SkipReason
    tier: Tier | None = None


class PackedDiff(_Frozen):
    """Files selected for one model attempt (patches may be truncated)."""

    files: tuple[AnalysisFile, ...]
    skipped: tuple[SkippedFile, ...] = ()
    truncated_paths: tuple[str, ...] = ()
    tokens: int = 0


class ReviewContext(_Frozen):
    repo: str
    base_ref: str
    target_ref: str
    commits: tuple[Commit, ...] = ()


class GuardrailAction(_Frozen):
    rule: str  # "G1".."G9"
    detail: str


class TokenUsage(BaseModel):
    prompt: int = 0
    completion: int = 0


class LatencyMs(BaseModel):
    git: int | None = None
    llm: int = 0
    total: int | None = None


class RawMetrics(BaseModel):
    """Shape of ``review_jobs.raw_metrics``. Documented in docs/specs/M2-analysis-engine.md."""

    prompt_version: str
    engine_version: str
    model: str | None = None  # None when the LLM was skipped (short-circuit)
    fallback_used: bool = False
    attempts: int = 0
    files_total: int = 0
    files_analyzed: int = 0
    additions: int = 0
    deletions: int = 0
    coverage: Coverage = Coverage.FULL
    provider_truncated: bool = False
    skipped_files: list[SkippedFile] = Field(default_factory=list)
    truncated_files: list[str] = Field(default_factory=list)
    signals: dict[str, int] = Field(default_factory=dict)
    redactions: int = 0
    guardrail_actions: list[GuardrailAction] = Field(default_factory=list)
    attempt_errors: list[str] = Field(default_factory=list)
    tokens: TokenUsage = Field(default_factory=TokenUsage)
    cost_usd: float | None = None
    latency_ms: LatencyMs = Field(default_factory=LatencyMs)


class AnalysisResult(BaseModel):
    report: ReleaseReviewReport
    metrics: RawMetrics

    @property
    def verdict(self) -> Verdict:
        return self.report.verdict
