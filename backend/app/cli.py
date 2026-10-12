"""``rr`` operator CLI. Pilots are onboarded manually in Phase 1."""

import asyncio
import json
import os
import uuid
from typing import Annotated

import typer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.crypto import encrypt_secret, generate_api_token
from app.core.db import get_sessionmaker
from app.domain.enums import GitAuthType, GitProviderKind, JobStatus, UserRole
from app.models import ApiToken, GitConnection, Organization, Project, User
from app.models.review import ReviewHazard, ReviewJob, ReviewPrerequisite

app = typer.Typer(help="ReleaseRadar operator CLI", no_args_is_help=True)


@app.callback()
def main() -> None:
    """ReleaseRadar operator CLI."""


async def _seed(email: str, repo: str, provider: GitProviderKind, base_url: str) -> str:
    provider_token = os.environ.get("RR_SEED_PROVIDER_TOKEN", "replace-me")
    async with get_sessionmaker()() as session, session.begin():
        org = Organization(name="Demo Org")
        session.add(org)
        await session.flush()

        user = User(org_id=org.id, email=email, display_name="Demo Admin", role=UserRole.ADMIN)
        connection = GitConnection(
            org_id=org.id,
            provider=provider,
            base_url=base_url,
            auth_type=GitAuthType.ACCESS_TOKEN,
            account_login=repo.split("/")[0],
            access_token_encrypted=encrypt_secret(provider_token),
        )
        session.add_all([user, connection])
        await session.flush()

        session.add(
            Project(
                org_id=org.id,
                git_connection_id=connection.id,
                name=repo.split("/")[-1],
                repo_identifier=repo,
            )
        )
        issued = generate_api_token()
        session.add(
            ApiToken(
                user_id=user.id,
                name="seed",
                token_prefix=issued.display_prefix,
                token_hash=issued.token_hash,
            )
        )
    return issued.plaintext


@app.command()
def seed(
    email: Annotated[str, typer.Option(help="Admin user email")] = "admin@demo.local",
    repo: Annotated[str, typer.Option(help="Repository identifier")] = "octocat/Hello-World",
    provider: Annotated[
        GitProviderKind, typer.Option(help="Git provider")
    ] = GitProviderKind.GITHUB,
    base_url: Annotated[str, typer.Option(help="Provider API root")] = "https://api.github.com",
) -> None:
    """Create a demo org, admin user, git connection, project and print an access token.

    The provider token is read from RR_SEED_PROVIDER_TOKEN.
    """
    token = asyncio.run(_seed(email, repo, provider, base_url))
    typer.echo("Seeded demo data. ReleaseRadar access token (shown once):")
    typer.echo(token)


@app.command()
def openapi() -> None:
    """Print the OpenAPI schema (used by the frontend's `npm run gen:api`)."""
    from app.main import app as fastapi_app

    typer.echo(json.dumps(fastapi_app.openapi(), indent=2))


async def _find_project(session: AsyncSession, ref: str) -> Project:
    try:
        project = await session.get(Project, uuid.UUID(ref))
    except ValueError:
        projects = (await session.scalars(select(Project).where(Project.name == ref))).all()
        if len(projects) > 1:
            raise typer.BadParameter(
                f"{len(projects)} projects named {ref!r}; use the id"
            ) from None
        project = projects[0] if projects else None
    if project is None:
        raise typer.BadParameter(f"project {ref!r} not found")
    return project


async def _review(project_ref: str, base: str, target: str, enqueue: bool) -> uuid.UUID:
    from app.engine.pipeline import EngineSettings
    from app.services.reviews import build_llm_client, create_review_job, run_review

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        project = await _find_project(session, project_ref)
        job = await create_review_job(session, project.id, base, target)
        job_id = job.id
    if enqueue:
        from app.workers.tasks import run_review_task

        run_review_task.delay(str(job_id))
        return job_id
    settings = get_settings()
    await run_review(
        job_id,
        sessionmaker=sessionmaker,
        llm=build_llm_client(settings),
        engine_settings=EngineSettings.from_settings(settings),
    )
    return job_id


async def _print_job(job_id: uuid.UUID) -> None:
    async with get_sessionmaker()() as session:
        job = await session.get_one(ReviewJob, job_id)
        typer.echo(f"Job {job.id}: {job.status}")
        if job.status == JobStatus.FAILED:
            typer.echo(f"Error: {job.error_message}")
            return
        if job.status != JobStatus.COMPLETED:
            return
        metrics = job.raw_metrics
        typer.echo(f"Verdict: {job.verdict}  Risk: {job.risk_score}/100  Model: {job.llm_model}")
        typer.echo(f"Summary: {job.executive_summary}")
        typer.echo(f"Rollback safe: {job.rollback_safe} — {job.rollback_notes}")
        hazards = await session.scalars(
            select(ReviewHazard).where(ReviewHazard.review_job_id == job_id)
        )
        for h in hazards:
            breaking = f" BREAKING {h.change_type} {h.affected_symbol}" if h.is_breaking else ""
            typer.echo(f"  [{h.severity}] {h.category} {h.file_path}: {h.hazard_title}{breaking}")
        prereqs = await session.scalars(
            select(ReviewPrerequisite)
            .where(ReviewPrerequisite.review_job_id == job_id)
            .order_by(ReviewPrerequisite.sort_order)
        )
        for p in prereqs:
            typer.echo(f"  [ ] {p.kind}: {p.description}")
        typer.echo(
            f"Coverage: {metrics.get('coverage')}  Files analyzed: {metrics.get('files_analyzed')}"
            f"/{metrics.get('files_total')}  Tokens: {metrics.get('tokens')}"
            f"  Cost: ${metrics.get('cost_usd')}"
        )


@app.command()
def review(
    project: Annotated[str, typer.Option(help="Project id or name")],
    base: Annotated[str, typer.Option(help="Base ref (currently deployed), e.g. v2.4.0")],
    target: Annotated[str, typer.Option(help="Target ref to deploy, e.g. rc-2.5.0")],
    enqueue: Annotated[bool, typer.Option(help="Queue for the worker instead of running")] = False,
) -> None:
    """Run a pre-deploy review between two refs and print the report."""

    async def run() -> None:  # one event loop: pooled connections can't cross loops
        job_id = await _review(project, base, target, enqueue)
        if enqueue:
            typer.echo(f"Queued review job {job_id}")
        else:
            await _print_job(job_id)

    asyncio.run(run())


@app.command("eval")
def eval_(
    model: Annotated[
        str, typer.Option(help="primary | fallback | both | a LiteLLM model name")
    ] = "primary",
    case: Annotated[list[str] | None, typer.Option(help="Only run these case ids")] = None,
    cache: Annotated[bool, typer.Option(help="Reuse cached LLM responses")] = True,
    check: Annotated[bool, typer.Option(help="Exit 1 if any target is missed")] = False,
) -> None:
    """Run the eval suite (evals/cases + evals/private) and write results."""
    from app.engine.pipeline import EngineSettings
    from app.evals.runner import (
        EVALS_DIR,
        CachingLLMClient,
        format_table,
        load_cases,
        run_model,
        write_results,
    )
    from app.evals.scoring import failed_targets
    from app.services.reviews import build_llm_client

    settings = get_settings()
    configured = EngineSettings.from_settings(settings)
    primary, fallback = configured.primary_model, configured.fallback_model
    aliases: dict[str, list[str | None]] = {
        "primary": [primary],
        "fallback": [fallback],
        "both": [primary, fallback if fallback != primary else None],
    }
    models = [m for m in aliases.get(model, [model]) if m]
    if not models:
        raise typer.BadParameter(f"no model configured for {model!r}")
    cases = load_cases(only=case or ())
    if not cases:
        raise typer.BadParameter("no eval cases found")

    llm = CachingLLMClient(build_llm_client(settings), EVALS_DIR / ".cache", enabled=cache)
    failures: list[str] = []
    for name in models:
        summary, _ = asyncio.run(run_model(cases, name, llm))
        typer.echo(format_table(summary))
        typer.echo(f"Results: {write_results(summary)}  (cache hits: {llm.hits})\n")
        failures += [f"{name}: {f}" for f in failed_targets(summary.aggregate)]
    if failures:
        typer.echo("Missed targets:\n  " + "\n  ".join(failures))
        if check:
            raise typer.Exit(1)


if __name__ == "__main__":
    app()
