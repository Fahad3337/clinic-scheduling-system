"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse

from app.api.deps import WebAuthRequired
from app.api.v1.router import api_router
from app.core.config import get_settings
from app.web.dashboard import router as dashboard_router


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        debug=settings.debug,
    )
    app.include_router(api_router)
    app.include_router(dashboard_router)

    # WebAuthRequired -> a redirect to the login page, not a 401 JSON
    # body -- see get_current_staff_from_cookie's docstring in
    # app/api/deps.py. The JSON API's own 401s are untouched; this
    # handler only fires for the cookie-based dashboard dependency.
    @app.exception_handler(WebAuthRequired)
    async def _web_auth_required(request: Request, exc: WebAuthRequired) -> RedirectResponse:
        return RedirectResponse(url="/dashboard/login", status_code=303)

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
