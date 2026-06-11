"""/api/v1 router aggregation.

Auth lockdown is centralized here rather than per-route: the ``auth`` subrouter
(status/login/logout/password) is the only public surface under ``/api/v1`` --
the UI bootstraps from ``/auth/status`` and logs in via ``/auth/login``.
Everything else is mounted under a ``require_admin`` group so a newly added
router is authenticated by default (``require_admin`` is a no-op in convenience
mode, i.e. when no password is set). The public podcast/ops surfaces -- ``/rss``,
``/media``, ``/health`` -- are separate app-level routers outside ``/api/v1``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import require_admin
from app.api.v1 import auth as auth_routes
from app.api.v1 import corrections as corrections_routes
from app.api.v1 import episodes as episodes_routes
from app.api.v1 import feed as feed_routes
from app.api.v1 import jobs as jobs_routes
from app.api.v1 import llm as llm_routes
from app.api.v1 import prompt as prompt_routes
from app.api.v1 import purge as purge_routes
from app.api.v1 import reference as reference_routes
from app.api.v1 import settings as settings_routes
from app.api.v1 import source_fallbacks as source_fallbacks_routes
from app.api.v1 import status as status_routes
from app.api.v1 import submit as submit_routes
from app.api.v1 import uploads as uploads_routes

router = APIRouter(prefix="/api/v1")

# Public: the auth bootstrap surface (no session required).
router.include_router(auth_routes.router)

# Default-closed: a single require_admin gate covers every route below, so the
# admin API can't accidentally ship an unauthenticated endpoint.
admin = APIRouter(dependencies=[Depends(require_admin)])
admin.include_router(submit_routes.router)
admin.include_router(uploads_routes.router)
admin.include_router(status_routes.router)
admin.include_router(prompt_routes.router)
admin.include_router(corrections_routes.router)
admin.include_router(source_fallbacks_routes.router)
admin.include_router(purge_routes.router)
admin.include_router(feed_routes.router)
admin.include_router(settings_routes.router)
admin.include_router(episodes_routes.router)
admin.include_router(jobs_routes.router)
admin.include_router(reference_routes.router)
admin.include_router(llm_routes.router)
router.include_router(admin)
