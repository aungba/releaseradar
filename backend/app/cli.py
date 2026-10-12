"""``rr`` operator CLI. Pilots are onboarded manually in Phase 1."""

import asyncio
import json
import os
from typing import Annotated

import typer

from app.core.crypto import encrypt_secret, generate_api_token
from app.core.db import get_sessionmaker
from app.domain.enums import GitAuthType, GitProviderKind, UserRole
from app.models import ApiToken, GitConnection, Organization, Project, User

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


if __name__ == "__main__":
    app()
