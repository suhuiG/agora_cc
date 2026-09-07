"""publish 라우터 — repo 연결/검증. write는 admin 게이트, GET read는 공개."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..sourcestore.audit import AuditContext
from ...governance.authz import ADMIN_ONLY, require_role

router = APIRouter()


class ConnectRequest(BaseModel):
    provider: str = "github"
    repo_url: str
    token: str


class ConnectionResponse(BaseModel):
    provider: str
    repo_url: str
    status: str
    connected_at: str


def _principal(request: Request) -> str:
    return AuditContext.from_request(request).principal()


@router.post("/api/publish/connection", response_model=ConnectionResponse,
             dependencies=[Depends(require_role(*ADMIN_ONLY))])
def set_connection(req: ConnectRequest, request: Request):
    from ....shared.deps import get_connection_service
    if not req.repo_url.strip() or not req.token.strip():
        raise HTTPException(422, "repo_url과 token이 필요해요")
    try:
        conn = get_connection_service().connect(
            req.provider, req.repo_url, req.token, principal=_principal(request))
    except ConnectionError as e:
        raise HTTPException(422, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return ConnectionResponse(
        provider=conn.provider, repo_url=conn.repo_url,
        status=conn.status, connected_at=conn.connected_at)


@router.get("/api/publish/connection")
def get_connection():
    from ....shared.deps import get_connection_service
    conn = get_connection_service().get()
    if conn is None:
        return {"status": "NOT_CONNECTED"}
    return {"provider": conn.provider, "repo_url": conn.repo_url,
            "status": conn.status, "connected_at": conn.connected_at}


@router.post("/api/publish/connection/verify",
             dependencies=[Depends(require_role(*ADMIN_ONLY))])
def verify_connection():
    from ....shared.deps import get_connection_service
    try:
        res = get_connection_service().verify_current()
    except LookupError as e:
        raise HTTPException(404, str(e))
    return {"ok": res.ok, "reason": res.reason}
