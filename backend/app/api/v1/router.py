"""/api/v1 router aggregation."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import auth as auth_routes
from app.api.v1 import corrections as corrections_routes
from app.api.v1 import episodes as episodes_routes
from app.api.v1 import jobs as jobs_routes
from app.api.v1 import llm as llm_routes
from app.api.v1 import prompt as prompt_routes
from app.api.v1 import purge as purge_routes
from app.api.v1 import reference as reference_routes
from app.api.v1 import settings as settings_routes
from app.api.v1 import status as status_routes
from app.api.v1 import submit as submit_routes

router = APIRouter(prefix="/api/v1")
router.include_router(submit_routes.router)
router.include_router(status_routes.router)
router.include_router(prompt_routes.router)
router.include_router(corrections_routes.router)
router.include_router(purge_routes.router)
router.include_router(auth_routes.router)
router.include_router(settings_routes.router)
router.include_router(episodes_routes.router)
router.include_router(jobs_routes.router)
router.include_router(reference_routes.router)
router.include_router(llm_routes.router)
