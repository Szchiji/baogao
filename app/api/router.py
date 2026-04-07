from __future__ import annotations

from fastapi import APIRouter

from app.api.reports import router as reports_router
from app.api.templates import router as templates_router
from app.api.subscriptions import router as subscriptions_router

api_router = APIRouter(prefix="/api")
api_router.include_router(reports_router)
api_router.include_router(templates_router)
api_router.include_router(subscriptions_router)
