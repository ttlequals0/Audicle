"""/api/v1 router aggregation."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import status as status_routes
from app.api.v1 import submit as submit_routes

router = APIRouter(prefix="/api/v1")
router.include_router(submit_routes.router)
router.include_router(status_routes.router)
