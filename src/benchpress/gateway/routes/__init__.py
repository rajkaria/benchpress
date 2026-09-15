"""The gateway's HTTP routes, split by concern; `router` combines every route into one."""

from __future__ import annotations

from fastapi import APIRouter

from benchpress.gateway.routes.approvals import router as _approvals_router
from benchpress.gateway.routes.core import router as _core_router

__all__ = ["router"]

router = APIRouter()
router.include_router(_core_router)
router.include_router(_approvals_router)
