"""Eval runner: load cases, run the engine per model, score, write results (M2 plan §8).

Case formats (one directory per case under ``evals/cases`` or gitignored ``evals/private``):

* ``case.yaml``  — inline ``files`` (hand-written seed cases) plus ``expected``.
* ``compare.json`` + ``expected.yaml`` — a recorded ``CompareResult`` (real repos, pilots).
"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from app.domain.enums import ChangeType
from app.engine.llm import LLMClient, LLMResponse, Message
from app.engine.models import AnalysisResult, ReviewContext
from app.engine.pipeline import EngineError, EngineSettings, analyze
from app.engine.prompt import PROMPT_VERSION
from app.evals.scoring import AggregateScore, CaseScore, Expected, aggregate, score_case
from app.integrations.git.base import CompareResult, FileDiff
from app.integrations.git.diffutil import count_changes

EVALS_DIR = Path(__file__).resolve().parents[2] / "evals"


class InlineFile(BaseModel):
    path: str
    status: ChangeType = ChangeType.MODIFIED
    old_path: str | None = None
    patch: str | None = None
    is_binary: bool = False

    def to_diff(self) -> FileDiff:
        additions, deletions = count_changes(self.patch)
        return FileDiff(
            path=self.path, old_path=self.old_path, status=self.status, patch=self.patch,
            additions=additions, deletions=deletions, is_binary=self.is_binary,
        )  # fmt: skip


class Case(BaseModel):
    id: str
    description: str = ""
    repo: str = "example/app"
    base_ref: str = "v1.0.0"
    target_ref: str = "v1.1.0"
    files: list[InlineFile] = Field(default_factory=list)
    expected: Expected
    compare: CompareResult | None = None

    def compare_result(self) -> CompareResult:
        if self.compare is not None:
            return self.compare
        return CompareResult(
            base_sha="0" * 40,
            head_sha="1" * 40,
            commits=[],
            files=[f.to_diff() for f in self.files],
        )

    def context(self) -> ReviewContext:
        compare = self.compare_result()
        return ReviewContext(
            repo=self.repo, base_ref=self.base_ref, target_ref=self.target_ref,
            commits=tuple(compare.commits),
        )  # fmt: skip


def load_case(directory: Path) -> Case:
    if (directory / "case.yaml").exists():
        data = yaml.safe_load((directory / "case.yaml").read_text())
        return Case.model_validate({"id": directory.name, **data})
    expected = yaml.safe_load((directory / "expected.yaml").read_text())
    compare = CompareResult.model_validate_json((directory / "compare.json").read_text())
    meta = {
        k: expected.pop(k)
        for k in ("description", "repo", "base_ref", "target_ref")
        if k in expected
    }
    return Case.model_validate(
        {"id": directory.name, **meta, "expected": expected, "compare": compare}
    )


def load_cases(root: Path = EVALS_DIR, only: Sequence[str] = ()) -> list[Case]:
    dirs = [
        d
        for sub in ("cases", "private")
        if (root / sub).is_dir()
        for d in sorted((root / sub).iterdir())
    ]
    cases = [load_case(d) for d in dirs if d.is_dir() and not d.name.startswith((".", "_"))]
    return [c for c in cases if not only or c.id in only]


class CachingLLMClient:
    """Caches responses by (model, messages) so re-scoring after scoring changes is free."""

    def __init__(self, inner: LLMClient, cache_dir: Path, *, enabled: bool = True) -> None:
        self._inner = inner
        self._dir = cache_dir
        self._enabled = enabled
        self.hits = 0

    def context_window(self, model: str) -> int:
        return self._inner.context_window(model)

    async def generate(
        self, *, model: str, messages: Sequence[Message], json_schema: dict[str, Any] | None
    ) -> LLMResponse:
        key = hashlib.sha256(
            json.dumps([model, list(messages)], sort_keys=True).encode()
        ).hexdigest()
        path = self._dir / f"{key}.json"
        if self._enabled and path.exists():
            self.hits += 1
            return LLMResponse(**json.loads(path.read_text()))
        response = await self._inner.generate(
            model=model, messages=messages, json_schema=json_schema
        )
        self._dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(response)))
        return response


@dataclass
class CaseRun:
    case: Case
    result: AnalysisResult | None
    score: CaseScore | None
    error: str | None = None


class ModelRun(BaseModel):
    model: str
    prompt_version: str
    started_at: datetime
    aggregate: AggregateScore
    cases: list[dict[str, Any]]
    cost_usd: float
    errors: int


async def run_model(
    cases: Sequence[Case], model: str, llm: LLMClient
) -> tuple[ModelRun, list[CaseRun]]:
    settings = EngineSettings(primary_model=model, fallback_model=None)
    started = datetime.now(UTC)
    runs: list[CaseRun] = []
    for case in cases:
        try:
            result = await analyze(case.compare_result(), case.context(), llm, settings)
        except EngineError as exc:
            runs.append(CaseRun(case, None, None, str(exc)))
            continue
        runs.append(CaseRun(case, result, score_case(case.id, result.report, case.expected)))

    scores = [r.score for r in runs if r.score is not None]
    summary = ModelRun(
        model=model,
        prompt_version=PROMPT_VERSION,
        started_at=started,
        aggregate=aggregate(scores),
        cases=[
            {
                "id": r.case.id,
                "error": r.error,
                "score": r.score.model_dump() if r.score else None,
                "verdict": r.result.report.verdict if r.result else None,
                "risk_score": r.result.report.risk_score if r.result else None,
                "metrics": r.result.metrics.model_dump(mode="json") if r.result else None,
            }
            for r in runs
        ],
        cost_usd=sum((r.result.metrics.cost_usd or 0.0) for r in runs if r.result),
        errors=sum(1 for r in runs if r.error),
    )
    return summary, runs


def write_results(run: ModelRun, root: Path = EVALS_DIR) -> Path:
    directory = root / "results" / run.prompt_version
    directory.mkdir(parents=True, exist_ok=True)
    slug = run.model.replace("/", "_")
    path = directory / f"{run.started_at:%Y%m%dT%H%M%SZ}-{slug}.json"
    path.write_text(run.model_dump_json(indent=2))
    return path


def format_table(run: ModelRun) -> str:
    lines = [f"Model {run.model} · prompt {run.prompt_version} · cost ${run.cost_usd:.4f}", ""]
    lines.append(f"{'case':38} {'verdict':24} {'recall':>6} {'FP':>3} {'rollback':>8} {'env':>5}")
    for case in run.cases:
        score = case["score"]
        if score is None:
            lines.append(f"{case['id']:38} ERROR: {case['error']}")
            continue
        recall = (
            score["matched_hazards"] / score["expected_hazards"]
            if score["expected_hazards"]
            else 1.0
        )
        rollback = {True: "ok", False: "WRONG", None: "-"}[score["rollback_ok"]]
        verdict_mark = {True: "", False: " ✗", None: ""}[score["verdict_ok"]]
        env = f"{score['env_found']}/{score['env_expected']}"
        lines.append(
            f"{case['id']:38} {str(case['verdict']) + verdict_mark:24} {recall:6.2f} "
            f"{score['false_positives']:3d} {rollback:>8} {env:>5}"
        )
    agg = run.aggregate
    lines += [
        "",
        f"Tier 1 recall {agg.tier1_recall:.2f} · recall {agg.recall:.2f} · "
        f"FP/case {agg.false_positives_per_case:.2f} · verdict {agg.verdict_accuracy:.2f} · "
        f"rollback {agg.rollback_accuracy:.2f} · env {agg.env_recall:.2f}",
    ]
    return "\n".join(lines)
