from fastapi import APIRouter

from app.api.v1 import auth, git_connections, health, projects, reviews, signoff

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(git_connections.router)
api_router.include_router(projects.router)
api_router.include_router(reviews.router)
api_router.include_router(signoff.router)
