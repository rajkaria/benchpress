"""Health, sessions, execute and policy routes. (Receipts routes live in `gateway.app.receipts_router`,
shared with `create_ui_app`.)
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from benchpress import __version__
from benchpress.gateway.deps import PolicyName, ServiceDep, WorkspaceDep
from benchpress.gateway.schemas import ExecuteRequest, ExecuteResponse, SessionCreate, SessionCreated
from benchpress.gateway.service import RequestRejected

__all__ = ["router"]

router = APIRouter()


@router.get("/healthz")
async def healthz(service: ServiceDep) -> JSONResponse:
    state = "ok" if await service.store_healthy() else "error"
    body = {"status": state, "version": __version__, "store": state}
    return JSONResponse(body, status_code=200 if state == "ok" else 503)


@router.get("/v1/meta")
async def meta(service: ServiceDep, _workspace: WorkspaceDep) -> dict[str, str]:
    return {"mode": "gateway", "version": __version__, "auth": service.settings.auth}


@router.post("/v1/sessions", status_code=201)
async def create_session(body: SessionCreate, workspace: WorkspaceDep, service: ServiceDep) -> SessionCreated:
    return await service.create_session(workspace, body)


@router.post("/v1/execute")
async def execute(
    body: ExecuteRequest, workspace: WorkspaceDep, service: ServiceDep, response: Response
) -> ExecuteResponse:
    result = await service.execute(workspace, body)
    if result.status == "needs_approval":
        response.status_code = 202
    return result


@router.get("/v1/policies")
async def list_policies(workspace: WorkspaceDep, service: ServiceDep) -> dict[str, object]:
    return await service.policies(workspace)


@router.put("/v1/policies/{name}")
async def put_policy(
    name: PolicyName, request: Request, workspace: WorkspaceDep, service: ServiceDep
) -> dict[str, str]:
    try:
        text = (await request.body()).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RequestRejected(422, "a policy pack is UTF-8 YAML") from exc
    return await service.put_policy(workspace, name, text)


@router.delete("/v1/policies/{name}", status_code=204)
async def delete_policy(name: PolicyName, workspace: WorkspaceDep, service: ServiceDep) -> None:
    await service.delete_policy(workspace, name)
