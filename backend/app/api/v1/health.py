from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok"]


class ReadinessResponse(BaseModel):
    status: Literal["ok", "degraded"]
    database: bool


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness: the process is up."""
    return HealthResponse(status="ok")


@router.get("/ready", response_model=ReadinessResponse)
async def ready(session: Annotated[AsyncSession, Depends(get_session)]) -> ReadinessResponse:
    """Readiness: dependencies are reachable."""
    try:
        await session.execute(text("SELECT 1"))
        database = True
    except Exception:
        database = False
    return ReadinessResponse(status="ok" if database else "degraded", database=database)
