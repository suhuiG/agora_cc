"""MCP 도구 목록 드리프트 · 민감도 확정 API (관리자 전용).

    GET  /api/mcp/tool-drift                         원장만 읽어요(네트워크 없음)
    POST /api/mcp/tool-drift/{rid}/resync            "다시 읽기" — tools/list 재조회
    POST /api/mcp/tool-drift/{rid}/tools/{t}/preview 바꾸기 전 영향 미리보기
    PUT  /api/mcp/tool-drift/{rid}/tools/{t}/sensitivity  관리자 확정
    GET  /api/mcp/tool-drift/{rid}/history           민감도 변경 이력

`require_role`은 governance 도메인에 있는 플랫폼 RBAC 공용 의존성이에요. identity 도메인도
같은 방식으로 가져다 써요(`identity/users_router.py`) — 관리자 게이트를 도메인마다 새로
구현하지 않는 게 이 저장소의 규약이에요.

미리보기가 `POST` 인 건 body 로 후보 태그를 받기 때문이에요. 원장을 쓰지 않아요.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from ....shared.deps import get_current_principal
from ...governance.authz import ADMIN_ONLY, require_role
from .drift_service import (
    McpAssetNotFound,
    McpLedgerConflict,
    McpSensitivityChangeConflict,
    McpSensitivityChangeNotFound,
    McpSensitivityImpactChanged,
    McpSensitivityPropagationFailed,
    McpToolNotFound,
)
from .drift_models import SensitivityChangeStatus
from .sensitivity_admin import SENSITIVITY_CHOICES, SensitivityChangeRejected

router = APIRouter(tags=["mcp-tool-drift"])

#: 거부 코드 → HTTP 상태. 사유 누락·잘못된 값은 422(입력 문제), 없는 도구는 409(상태 문제).
_REJECTION_STATUS = {
    "reason_required": 422,
    "invalid_sensitivity": 422,
    "tool_not_present": 409,
    "no_change": 409,
    "impact_unknown": 409,
    "impact_changed": 409,
    "agent_propagation_unavailable": 409,
    "target_propagation_pending": 409,
    "target_ledger_invalid": 409,
    "connected_sync_pending": 409,
}


def _service():
    from ....shared.deps import get_mcp_drift_service
    return get_mcp_drift_service()


def _rejected(exc: SensitivityChangeRejected) -> HTTPException:
    detail = {
        "code": exc.code,
        "message": exc.message,
        "remediation": exc.remediation,
        "choices": list(SENSITIVITY_CHOICES),
    }
    if isinstance(exc, McpSensitivityImpactChanged):
        detail["change"] = exc.change.to_dict()
    return HTTPException(
        _REJECTION_STATUS.get(exc.code, 422),
        detail,
    )


@router.get("/api/mcp/tool-drift", dependencies=[Depends(require_role(*ADMIN_ONLY))])
def list_tool_drift():
    """등록된 MCP 자산별 도구 상태와 마지막 확인 시각.

    여기서는 MCP 를 새로 떠오지 않아요 — 화면을 열 때마다 외부 endpoint 를 두드리면
    목록 조회가 남의 서버 응답 시간에 묶여요. 재조회는 명시적인 resync 예요.
    """
    return {"assets": [snapshot.to_dict() for snapshot in _service().snapshot()]}


@router.post(
    "/api/mcp/tool-drift/{record_id}/resync",
    dependencies=[Depends(require_role(*ADMIN_ONLY))],
)
def resync_tool_drift(record_id: str):
    """`tools/list`를 다시 떠와 원장과 대조해요.

    MCP 가 응답하지 않아도 200 이에요 — 대신 `check_status=unknown`과 이유가 담겨요.
    관측 실패를 성공으로도, HTTP 에러로도 뭉개지 않아요(전자는 거짓말이고 후자는
    "드리프트 없음"과 구분이 안 돼요).
    """
    try:
        return _service().resync(record_id).to_dict()
    except McpAssetNotFound:
        raise HTTPException(404, "MCP 자산을 찾지 못했어요.")


@router.post(
    "/api/mcp/tool-drift/{record_id}/tools/{tool_name}/preview",
    dependencies=[Depends(require_role(*ADMIN_ONLY))],
)
def preview_sensitivity(record_id: str, tool_name: str, body: dict):
    """태그를 바꾸기 **전에** 결과를 계산해요 — 원장을 쓰지 않아요.

    관리자가 고르는 건 숫자가 아니라 결과예요. 어느 등급이 이 도구를 얻고 잃는지,
    사유가 필요한 변경인지, 이미 이 도구를 인가받은 agent 가 몇 개인지를 함께 돌려줘요.
    """
    try:
        plan = _service().plan_sensitivity(
            record_id, tool_name, body.get("sensitivity"))
    except McpAssetNotFound:
        raise HTTPException(404, "MCP 자산을 찾지 못했어요.")
    except McpToolNotFound:
        raise HTTPException(404, "원장에 그 도구가 없어요.")
    except SensitivityChangeRejected as exc:
        raise _rejected(exc) from exc
    return plan.to_dict()


@router.put(
    "/api/mcp/tool-drift/{record_id}/tools/{tool_name}/sensitivity",
    dependencies=[Depends(require_role(*ADMIN_ONLY))],
)
def put_sensitivity(record_id: str, tool_name: str, body: dict, request: Request):
    """관리자가 민감도를 확정해요.

    `sensitivity`를 `null`로 보내면 미분류로 되돌려요. 배포형은 어느 Target에도 들어가지
    않지만 연결형의 live 차단은 별도 관측 전까지 unknown이에요. 위험도를 낮추는 변경에는
    `reason`이 필요하고, 없으면 422 로 **막아요** — 이 태그가 곧 인가 조건이라 조용히
    통과시키면 등록자가 낮게 태깅한 값이 그대로 굳어요.
    """
    _principal = get_current_principal(request)
    try:
        snapshot, plan, change = _service().set_sensitivity(
            record_id,
            tool_name,
            body.get("sensitivity"),
            reason=str(body.get("reason") or ""),
            actor=_principal.principal_id,
            actor_label=_principal.email or "",
        )
    except McpAssetNotFound:
        raise HTTPException(404, "MCP 자산을 찾지 못했어요.")
    except McpToolNotFound:
        raise HTTPException(404, "원장에 그 도구가 없어요.")
    except McpLedgerConflict as exc:
        # 낙관적 잠금 충돌 — admin 이 읽은 뒤 다른 변경(폴러 등)이 먼저 반영됐어요.
        raise HTTPException(409, {
            "code": "ledger_conflict",
            "message": "다른 변경이 먼저 적용됐어요 — 최신 상태를 다시 불러 확인해 주세요.",
        }) from exc
    except McpSensitivityChangeConflict as exc:
        raise HTTPException(409, {
            "code": "sensitivity_change_conflict",
            "message": "이 도구에 진행 중인 변경이 있거나 상태가 달라졌어요.",
        }) from exc
    except McpSensitivityPropagationFailed as exc:
        raise HTTPException(502, {
            "code": "sensitivity_propagation_failed",
            "message": exc.change.error,
            "change": exc.change.to_dict(),
        }) from exc
    except SensitivityChangeRejected as exc:
        raise _rejected(exc) from exc
    response = {
        "asset": snapshot.to_dict(),
        "plan": plan.to_dict(),
        "change": change.to_dict(),
        # 요청/전이/최종 커밋은 각 단계에서 감사와 원자적으로 저장돼요.
        "history_recorded": True,
    }
    if change.status in {
        SensitivityChangeStatus.PENDING_APPROVAL,
        SensitivityChangeStatus.APPROVED_PENDING_PROPAGATION,
    }:
        return JSONResponse(status_code=202, content=response)
    return response


def _change_action(
    record_id: str,
    tool_name: str,
    request_id: str,
    request: Request,
    *,
    action: str,
):
    principal = get_current_principal(request)
    try:
        operation = (
            _service().approve_sensitivity_change
            if action == "approve"
            else _service().retry_sensitivity_change
        )
        snapshot, plan, change = operation(
            record_id,
            tool_name,
            request_id,
            actor=principal.principal_id,
            actor_label=principal.email or "",
        )
    except McpAssetNotFound:
        raise HTTPException(404, "MCP 자산을 찾지 못했어요.")
    except McpToolNotFound:
        raise HTTPException(404, "원장에 그 도구가 없어요.")
    except McpSensitivityChangeNotFound:
        raise HTTPException(404, {
            "code": "sensitivity_change_not_found",
            "message": "민감도 변경 요청을 찾지 못했어요.",
        })
    except McpSensitivityChangeConflict as exc:
        raise HTTPException(409, {
            "code": "sensitivity_change_conflict",
            "message": "요청 상태나 도구 원장이 달라졌어요. 최신 상태를 다시 확인해 주세요.",
        }) from exc
    except McpSensitivityPropagationFailed as exc:
        raise HTTPException(502, {
            "code": "sensitivity_propagation_failed",
            "message": exc.change.error,
            "change": exc.change.to_dict(),
        }) from exc
    except SensitivityChangeRejected as exc:
        raise _rejected(exc) from exc
    response = {
        "asset": snapshot.to_dict(),
        "plan": plan.to_dict(),
        "change": change.to_dict(),
        "history_recorded": True,
    }
    if change.status is SensitivityChangeStatus.APPROVED_PENDING_PROPAGATION:
        return JSONResponse(status_code=202, content=response)
    return response


@router.post(
    "/api/mcp/tool-drift/{record_id}/tools/{tool_name}/"
    "sensitivity-changes/{request_id}/approve",
    dependencies=[Depends(require_role(*ADMIN_ONLY))],
)
def approve_sensitivity_change(
    record_id: str,
    tool_name: str,
    request_id: str,
    request: Request,
):
    return _change_action(
        record_id,
        tool_name,
        request_id,
        request,
        action="approve",
    )


@router.post(
    "/api/mcp/tool-drift/{record_id}/tools/{tool_name}/"
    "sensitivity-changes/{request_id}/retry",
    dependencies=[Depends(require_role(*ADMIN_ONLY))],
)
def retry_sensitivity_change(
    record_id: str,
    tool_name: str,
    request_id: str,
    request: Request,
):
    return _change_action(
        record_id,
        tool_name,
        request_id,
        request,
        action="retry",
    )


@router.get(
    "/api/mcp/tool-drift/{record_id}/history",
    dependencies=[Depends(require_role(*ADMIN_ONLY))],
)
def list_sensitivity_history(record_id: str):
    """민감도 변경 이력(최신순). append-only 라 수정·삭제 경로가 없어요."""
    try:
        events = _service().history(record_id)
    except McpAssetNotFound:
        raise HTTPException(404, "MCP 자산을 찾지 못했어요.")
    return {"events": [event.to_dict() for event in events]}
