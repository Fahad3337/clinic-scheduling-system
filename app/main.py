"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI

from app.api.v1.router import api_router
from app.core.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        debug=settings.debug,
    )
    app.include_router(api_router)

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        # WHY this hits nothing but returns 200: a liveness check should
        # answer "is the process up", not "is the database up" -- conflating
        # the two means a DB blip takes the container out of rotation even
        # though restarting it wouldn't help. Add a separate /readiness probe
        # (that does query the DB) when you wire this into orchestration.
        return {"status": "ok"}

    return app


app = create_app()
