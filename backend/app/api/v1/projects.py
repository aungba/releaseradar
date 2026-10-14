"""Projects & ref autocomplete endpoints (/api/v1/projects)."""

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.git_connections import (
    ProviderFactory,
    get_org_connection,
    get_provider_factory,
    map_git_provider_error,
)
from app.core.auth import AdminAuth, CurrentAuth
from app.core.db import get_session
from app.integrations.git import GitProviderError
from app.models.git import GitConnection, Project
from app.schemas.api import CreateProjectRequest, ProjectResponse, RefResponse
from app.services.git_connections import token_health

router = APIRouter(prefix="/projects", tags=["projects"])


def _to_project_response(
    project: Project, conn: GitConnection | None = None, *, now: datetime | None = None
) -> ProjectResponse:
    return ProjectResponse(
        id=project.id,
        org_id=project.org_id,
        git_connection_id=project.git_connection_id,
        name=project.name,
        repo_identifier=project.repo_identifier,
        external_repo_id=project.external_repo_id,
        default_branch=project.default_branch,
        is_active=project.is_active,
        provider=conn.provider if conn else None,
        token_health=token_health(conn.token_expires_at, now=now) if conn else None,
        created_at=project.created_at,
    )


async def get_org_project(
    session: AsyncSession, project_id: uuid.UUID, org_id: uuid.UUID
) -> tuple[Project, GitConnection]:
    row = (
        await session.execute(
            select(Project, GitConnection)
            .join(GitConnection, Project.git_connection_id == GitConnection.id)
            .where(Project.id == project_id, Project.org_id == org_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")
    return row[0], row[1]


@router.get("", response_model=list[ProjectResponse])
async def list_projects(
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[ProjectResponse]:
    """List active projects in the current organization ordered by name."""
    rows = (
        await session.execute(
            select(Project, GitConnection)
            .join(GitConnection, Project.git_connection_id == GitConnection.id)
            .where(Project.org_id == ctx.org.id, Project.is_active.is_(True))
            .order_by(Project.name.asc(), Project.id.asc())
        )
    ).all()
    now = datetime.now(UTC)
    return [_to_project_response(proj, conn, now=now) for proj, conn in rows]


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(
    body: CreateProjectRequest,
    ctx: AdminAuth,
    session: Annotated[AsyncSession, Depends(get_session)],
    provider_factory: Annotated[ProviderFactory, Depends(get_provider_factory)],
) -> ProjectResponse:
    """Register a repository under an organization git connection (admin only)."""
    conn = await get_org_connection(session, body.git_connection_id, ctx.org.id)
    try:
        async with provider_factory(conn) as provider:
            repo_info = await provider.get_repository(body.repo_identifier)
    except GitProviderError as exc:
        raise map_git_provider_error(exc) from None

    name = (body.name or "").strip() or body.repo_identifier.split("/")[-1]
    project = Project(
        org_id=ctx.org.id,
        git_connection_id=conn.id,
        name=name[:100],
        repo_identifier=body.repo_identifier,
        external_repo_id=repo_info.external_id,
        default_branch=repo_info.default_branch or "main",
        is_active=True,
    )
    session.add(project)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Repository {body.repo_identifier!r} is already registered on this connection.",
        ) from None
    await session.refresh(project)
    return _to_project_response(project, conn)


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ProjectResponse:
    """Return a single project in the current organization."""
    project, conn = await get_org_project(session, project_id, ctx.org.id)
    return _to_project_response(project, conn)


@router.get("/{project_id}/refs", response_model=list[RefResponse])
async def list_project_refs(
    project_id: uuid.UUID,
    ctx: CurrentAuth,
    session: Annotated[AsyncSession, Depends(get_session)],
    provider_factory: Annotated[ProviderFactory, Depends(get_provider_factory)],
    q: Annotated[str, Query(max_length=200, description="Branch or tag prefix filter")] = "",
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> list[RefResponse]:
    """Autocomplete branches and tags for a project from its Git provider."""
    project, conn = await get_org_project(session, project_id, ctx.org.id)
    try:
        async with provider_factory(conn) as provider:
            refs = await provider.list_refs(project.repo_identifier, query=q.strip(), limit=limit)
    except GitProviderError as exc:
        raise map_git_provider_error(exc) from None
    return [RefResponse(name=r.name, kind=r.kind, sha=r.sha) for r in refs]
