"""Approval queue routes: list, inspect, and resolve (approve, deny, or discover it closed/expired)."""

from __future__ import annotations

from fastapi import APIRouter, Request

from benchpress.gateway.deps import ServiceDep, WorkspaceDep, actor
from benchpress.gateway.schemas import ApprovalDecision, ApprovalView, ExecuteResponse

__all__ = ["router"]

router = APIRouter()


@router.get("/v1/approvals")
async def list_approvals(
    workspace: WorkspaceDep, service: ServiceDep, status: str | None = None
) -> list[ApprovalView]:
    return await service.list_approvals(workspace, status)


@router.get("/v1/approvals/{approval_id}")
async def get_approval(approval_id: str, workspace: WorkspaceDep, service: ServiceDep) -> ApprovalView:
    return await service.get_approval(workspace, approval_id)


@router.post("/v1/approvals/{approval_id}")
async def resolve_approval(
    approval_id: str, body: ApprovalDecision, workspace: WorkspaceDep, service: ServiceDep, request: Request
) -> ExecuteResponse:
    return await service.resolve_approval(workspace, approval_id, body, by=actor(request))
