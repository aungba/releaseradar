"""The analysis pipeline: ``CompareResult`` → validated, guard-railed ``ReleaseReviewReport``."""

import json
import re
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Self

from pydantic import ValidationError

from app.core.config import Settings
from app.domain.enums import Verdict
from app.engine import filters, guardrails
from app.engine.budget import MIN_USEFUL_TOKENS, TokenCounter, make_counter, pack
from app.engine.llm import LLMClient, LLMContextWindowError, LLMError
from app.engine.models import (
    AnalysisFile,
    AnalysisResult,
    Coverage,
    GuardrailAction,
    PackedDiff,
    RawMetrics,
    ReviewContext,
    Signal,
    SkippedFile,
    SkipReason,
    Tier,
    TokenUsage,
)
from app.engine.prompt import PROMPT_VERSION, DiffStats, Message, build_messages, report_schema
from app.engine.redact import redact
from app.engine.signals import detect
from app.engine.tiering import path_tier, promote
from app.integrations.git.base import CompareResult, FileDiff
from app.schemas.llm_contract import DeploymentPrerequisites, ReleaseReviewReport

ENGINE_VERSION = "0.2.0"
_CONTEXT_RETRY_FACTOR = 0.6  # on a context-window error, retry the same model with less input
_MAX_REPAIR_ECHO = 20_000
_REPAIR_PROMPT = (
    "Your previous response was not a valid report: {error}\n"
    "Return ONLY the corrected JSON object that matches the schema."
)


class EngineError(Exception):
    """Analysis failed. The message is safe to show users and store in ``error_message``."""


@dataclass(frozen=True)
class EngineSettings:
    primary_model: str
    fallback_model: str | None = None
    input_cap_primary: int = 150_000
    input_cap_fallback: int = 90_000
    context_utilization: float = 0.7

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(
            primary_model=settings.llm_primary_model,
            fallback_model=settings.llm_fallback_model or None,
            input_cap_primary=settings.llm_input_cap_primary,
            input_cap_fallback=settings.llm_input_cap_fallback,
        )

    def models(self) -> list[str]:
        models = [self.primary_model]
        if self.fallback_model and self.fallback_model != self.primary_model:
            models.append(self.fallback_model)
        return models


# ---------------------------------------------------------------- deterministic preparation


@dataclass
class Prepared:
    files: list[AnalysisFile]  # analyzable, redacted, tiered
    filtered: list[SkippedFile]  # noise / binary
    omitted: list[SkippedFile]  # PATCH_OMITTED (changed, content unavailable)
    signals: list[Signal]
    redactions: int
    stats: DiffStats

    @property
    def has_production_changes(self) -> bool:
        return any(f.tier <= Tier.CORE for f in self.files) or any(
            s.tier is not None and s.tier <= Tier.CORE for s in self.omitted
        )


def prepare(compare: CompareResult) -> Prepared:
    kept, filtered = filters.partition(compare.files)
    tiers = {f.path: path_tier(f.path) for f in kept}

    analyzable: list[FileDiff] = []
    omitted: list[SkippedFile] = []
    redactions = 0
    for file in kept:
        if file.patch_omitted:
            omitted.append(
                SkippedFile(path=file.path, reason=SkipReason.PATCH_OMITTED, tier=tiers[file.path])
            )
            continue
        redacted, count = redact(file)
        redactions += count
        analyzable.append(redacted)

    # Signals in tests/docs are noise (a test reading an env var is not a deploy requirement).
    production = [f for f in kept if tiers[f.path] <= Tier.CORE]
    production = [next((a for a in analyzable if a.path == f.path), f) for f in production]
    signals = detect(production)
    by_file: dict[str, list[Signal]] = {}
    for signal in signals:
        by_file.setdefault(signal.file_path, []).append(signal)

    files = []
    for file in analyzable:
        file_signals = tuple(by_file.get(file.path, ()))
        files.append(
            AnalysisFile(
                diff=file, tier=promote(tiers[file.path], file_signals), signals=file_signals
            )
        )
    stats = DiffStats(
        files_total=len(compare.files),
        additions=sum(f.additions for f in compare.files),
        deletions=sum(f.deletions for f in compare.files),
    )
    return Prepared(files, filtered, omitted, signals, redactions, stats)


def parse_report(text: str) -> ReleaseReviewReport:
    """Parse model output, tolerating code fences or prose around the JSON object."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end < start:
        raise ValueError("response contains no JSON object")
    return ReleaseReviewReport.model_validate(json.loads(cleaned[start : end + 1]))


def _short_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        parts = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:10]]
        return "; ".join(parts)
    return str(exc)[:500]


# ---------------------------------------------------------------- run state


@dataclass
class _Run:
    attempts: int = 0
    errors: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float | None = None
    llm_ms: int = 0

    def record(self, prompt: int, completion: int, cost: float | None, ms: int) -> None:
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.llm_ms += ms
        if cost is not None:
            self.cost = (self.cost or 0.0) + cost


@dataclass(frozen=True)
class _Success:
    report: ReleaseReviewReport
    model: str
    packed: PackedDiff
    fallback_used: bool


# ---------------------------------------------------------------- pipeline


async def analyze(
    compare: CompareResult,
    context: ReviewContext,
    llm: LLMClient,
    settings: EngineSettings,
    *,
    counter_factory: Callable[[str], TokenCounter] = make_counter,
) -> AnalysisResult:
    started = time.monotonic()
    prepared = prepare(compare)
    changed_paths = {f.path for f in compare.files} | {
        f.old_path for f in compare.files if f.old_path
    }

    if not prepared.has_production_changes:
        return _short_circuit(prepared, compare)

    run = _Run()
    success = await _call_models(prepared, compare, context, llm, settings, counter_factory, run)

    not_analyzed = [
        s
        for s in (*prepared.omitted, *success.packed.skipped)
        if s.tier is not None and s.tier <= Tier.CORE
    ]
    partial = bool(not_analyzed) or compare.truncated
    report, actions = guardrails.apply(
        success.report,
        changed_paths=changed_paths,
        signals=prepared.signals,
        partial=partial,
        not_analyzed_count=len(not_analyzed),
    )
    metrics = _metrics(prepared, compare, actions, partial=partial)
    metrics.model = success.model
    metrics.fallback_used = success.fallback_used
    metrics.attempts = run.attempts
    metrics.attempt_errors = run.errors
    metrics.files_analyzed = len(success.packed.files)
    metrics.skipped_files += list(success.packed.skipped)
    metrics.truncated_files = list(success.packed.truncated_paths)
    metrics.tokens = TokenUsage(prompt=run.prompt_tokens, completion=run.completion_tokens)
    metrics.cost_usd = run.cost
    metrics.latency_ms.llm = run.llm_ms
    metrics.latency_ms.total = int((time.monotonic() - started) * 1000)
    return AnalysisResult(report=report, metrics=metrics)


async def _call_models(
    prepared: Prepared,
    compare: CompareResult,
    context: ReviewContext,
    llm: LLMClient,
    settings: EngineSettings,
    counter_factory: Callable[[str], TokenCounter],
    run: _Run,
) -> _Success:
    not_shown = [*prepared.filtered, *prepared.omitted]

    def messages_for(packed: PackedDiff) -> list[Message]:
        return build_messages(
            context,
            packed,
            stats=prepared.stats,
            signals=prepared.signals,
            not_shown=not_shown,
            provider_truncated=compare.truncated,
        )

    for index, model in enumerate(settings.models()):
        count = counter_factory(model)
        cap = settings.input_cap_primary if index == 0 else settings.input_cap_fallback
        window = min(int(llm.context_window(model) * settings.context_utilization), cap)
        overhead = sum(count(m["content"]) for m in messages_for(PackedDiff(files=())))
        factor = 1.0
        while True:
            budget = int((window - overhead) * factor)
            if budget < MIN_USEFUL_TOKENS:
                run.errors.append(f"{model}: input budget too small ({budget} tokens)")
                break
            packed = pack(prepared.files, budget, count)
            try:
                report = await _generate_with_repair(llm, model, messages_for(packed), run)
            except LLMContextWindowError as exc:
                run.errors.append(str(exc))
                if factor == 1.0:
                    factor = _CONTEXT_RETRY_FACTOR
                    continue
                break
            except LLMError as exc:
                run.errors.append(str(exc))
                break
            if report is None:
                break  # invalid twice → next model
            return _Success(report=report, model=model, packed=packed, fallback_used=index > 0)

    raise EngineError("All LLM providers failed: " + "; ".join(run.errors))


async def _generate_with_repair(
    llm: LLMClient, model: str, messages: Sequence[Message], run: _Run
) -> ReleaseReviewReport | None:
    """One call plus one repair retry. Returns None if both responses are invalid."""
    conversation = list(messages)
    for attempt in range(2):
        run.attempts += 1
        response = await llm.generate(
            model=model, messages=conversation, json_schema=report_schema()
        )
        run.record(
            response.prompt_tokens,
            response.completion_tokens,
            response.cost_usd,
            response.latency_ms,
        )
        try:
            return parse_report(response.text)
        except (ValueError, ValidationError) as exc:
            error = _short_error(exc)
            run.errors.append(f"{model}: invalid report ({error})")
            if attempt == 0:
                conversation += [
                    {"role": "assistant", "content": response.text[:_MAX_REPAIR_ECHO]},
                    {"role": "user", "content": _REPAIR_PROMPT.format(error=error)},
                ]
    return None


def _metrics(
    prepared: Prepared, compare: CompareResult, actions: list[GuardrailAction], *, partial: bool
) -> RawMetrics:
    return RawMetrics(
        prompt_version=PROMPT_VERSION,
        engine_version=ENGINE_VERSION,
        files_total=prepared.stats.files_total,
        additions=prepared.stats.additions,
        deletions=prepared.stats.deletions,
        coverage=Coverage.PARTIAL if partial else Coverage.FULL,
        provider_truncated=compare.truncated,
        skipped_files=[*prepared.filtered, *prepared.omitted],
        signals=dict(Counter(s.kind.value for s in prepared.signals)),
        redactions=prepared.redactions,
        guardrail_actions=actions,
    )


def _short_circuit(prepared: Prepared, compare: CompareResult) -> AnalysisResult:
    """No production code changed: approve without calling an LLM."""
    if prepared.files:
        summary = "Only tests, documentation or styles changed; no production code is affected."
    else:
        summary = (
            "No deployable code changes: only non-code files (e.g. lockfiles, assets) changed."
        )
    report = ReleaseReviewReport(
        verdict=Verdict.APPROVED,
        risk_score=0,
        executive_summary=summary,
        hazards=[],
        deployment_prerequisites=DeploymentPrerequisites(
            zero_downtime_rollback_safe=True, rollback_notes="No production code changes."
        ),
    )
    partial = compare.truncated
    if partial:
        report, actions = guardrails.apply(report, changed_paths=(), signals=(), partial=True)
    else:
        actions = []
    metrics = _metrics(prepared, compare, actions, partial=partial)
    metrics.files_analyzed = 0
    return AnalysisResult(report=report, metrics=metrics)
