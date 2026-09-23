"""Aggregates all v1 routers under one include."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.appointments import router as appointments_router
from app.api.v1.auth import router as auth_router
from app.api.v1.calendar import router as calendar_router
from app.api.v1.conversations import router as conversations_router
from app.api.v1.doctors import router as doctors_router
from app.api.v1.webhooks import router as webhooks_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(appointments_router)
api_router.include_router(doctors_router)
api_router.include_router(calendar_router)
api_router.include_router(auth_router)
api_router.include_router(webhooks_router)
api_router.include_router(conversations_router)
