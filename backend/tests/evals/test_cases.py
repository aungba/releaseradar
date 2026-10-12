"""Seed eval cases load, produce the expected deterministic signals, and run end to end."""

from pathlib import Path

from app.engine.llm import FakeLLMClient
from app.engine.models import SignalKind
from app.engine.pipeline import prepare
from app.evals.runner import (
    EVALS_DIR,
    CachingLLMClient,
    Case,
    format_table,
    load_case,
    load_cases,
    run_model,
    write_results,
)
from tests.engine.helpers import report_json

MODEL = "fake/model"


def _case(case_id: str) -> Case:
    return load_case(EVALS_DIR / "cases" / case_id)


def _signals(case_id: str) -> set[tuple[SignalKind, str | None]]:
    return {(s.kind, s.symbol) for s in prepare(_case(case_id).compare_result()).signals}


def test_all_seed_cases_load() -> None:
    cases = load_cases(EVALS_DIR, ())
    seed = [c for c in cases if (EVALS_DIR / "cases" / c.id).is_dir()]
    assert len(seed) >= 10
    assert len({c.id for c in cases}) == len(cases)
    for case in seed:
        assert case.files, case.id
        assert case.expected.verdict, f"{case.id} must state acceptable verdicts"


def test_load_cases_filters_by_id() -> None:
    assert [c.id for c in load_cases(EVALS_DIR, ["03-removed-endpoint"])] == ["03-removed-endpoint"]


def test_drop_column_case_signals() -> None:
    kinds = {kind for kind, _ in _signals("01-drop-column-still-read")}
    assert {SignalKind.DESTRUCTIVE_SQL, SignalKind.MIGRATION_ADDED} <= kinds


def test_env_var_case_signals() -> None:
    assert (SignalKind.ENV_VAR_ADDED, "STRIPE_WEBHOOK_SECRET") in _signals(
        "04-new-required-env-var"
    )


def test_removed_endpoint_case_signals() -> None:
    assert SignalKind.ROUTE_REMOVED in {kind for kind, _ in _signals("03-removed-endpoint")}


def test_prompt_injection_case_still_flags_destructive_sql() -> None:
    assert SignalKind.DESTRUCTIVE_SQL in {kind for kind, _ in _signals("10-prompt-injection")}


def test_compare_json_case_format(tmp_path: Path) -> None:
    source = _case("03-removed-endpoint")
    directory = tmp_path / "recorded"
    directory.mkdir()
    (directory / "compare.json").write_text(source.compare_result().model_dump_json())
    (directory / "expected.yaml").write_text(
        "description: recorded\nrepo: acme/api\nverdict: BLOCKED\n"
    )
    case = load_case(directory)
    assert case.repo == "acme/api"
    assert case.description == "recorded"
    assert case.compare_result().files == source.compare_result().files


async def test_docs_only_case_short_circuits_without_llm() -> None:
    llm = FakeLLMClient(script=[])
    summary, runs = await run_model([_case("09-docs-tests-only")], MODEL, llm)
    assert llm.calls == []
    assert runs[0].result is not None and runs[0].result.report.verdict == "APPROVED"
    assert summary.errors == 0


async def test_run_all_cases_with_fake_llm_and_cache(tmp_path: Path) -> None:
    cases = load_cases(EVALS_DIR, ())
    fake = FakeLLMClient(script=[report_json() for _ in cases])
    cached = CachingLLMClient(fake, tmp_path / "cache")

    first, runs = await run_model(cases, MODEL, cached)
    assert first.errors == 0
    assert all(r.score is not None for r in runs)
    calls = len(fake.calls)
    assert calls > 0

    second, _ = await run_model(cases, MODEL, cached)
    assert len(fake.calls) == calls  # served from cache
    assert cached.hits == calls
    assert second.aggregate == first.aggregate

    table = format_table(first)
    assert "Tier 1 recall" in table and "01-drop-column-still-read" in table
    path = write_results(first, tmp_path)
    assert path.parent == tmp_path / "results" / first.prompt_version
    assert path.read_text().startswith("{")


async def test_engine_errors_are_recorded_not_raised() -> None:
    llm = FakeLLMClient(script=["not json", "still not json"])
    summary, runs = await run_model([_case("03-removed-endpoint")], MODEL, llm)
    assert summary.errors == 1
    assert runs[0].score is None and runs[0].error


async def test_cache_can_be_disabled(tmp_path: Path) -> None:
    fake = FakeLLMClient(script=[report_json(), report_json()])
    cached = CachingLLMClient(fake, tmp_path, enabled=False)
    case = [_case("02-api-field-rename")]
    await run_model(case, MODEL, cached)
    await run_model(case, MODEL, cached)
    assert len(fake.calls) == 2 and cached.hits == 0
