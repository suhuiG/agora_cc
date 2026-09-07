"""콘솔 공통 라우트 — 권한 조회(me) + 설정(settings)."""
from __future__ import annotations

import datetime as _dt

from fastapi import APIRouter, Depends, Request, HTTPException

from ...shared.deps import backend_mode, get_gov_store, get_scan_service
from ..identity.context import current_principal
from .authz import ADMIN_ONLY, CONSOLE_ROLES, get_current_roles, require_role
from .models import ConsoleSettings
from .scan_applicability import ScanNotApplicableError

router = APIRouter(tags=["governance-console"])


@router.get("/api/governance/me")
def whoami(request: Request):
    principal = current_principal(request)
    roles = get_current_roles(request)
    is_admin = "admin" in roles
    return {
        "principal": principal.principal_id,
        "roles": list(roles),
        "can": {"tools_write": is_admin, "tier_write": is_admin, "review": is_admin},
        # 현재 백엔드 모드 — 콘솔이 mock/aws·scanner 종류를 배지로 표시해 모드 혼동을 막아요.
        "mode": backend_mode(),
    }


@router.get("/api/governance/settings", dependencies=[Depends(require_role(*CONSOLE_ROLES))])
def get_settings():
    return get_gov_store().get_settings().__dict__


@router.put("/api/governance/settings", dependencies=[Depends(require_role(*ADMIN_ONLY))])
def put_settings(body: dict, request: Request):
    store = get_gov_store()
    cur = store.get_settings()
    # asset_tier_map 부분 병합 + 값 검증(minimal/standard/strong).
    tmap = dict(cur.asset_tier_map)
    incoming = body.get("asset_tier_map")
    if incoming is not None:
        if not isinstance(incoming, dict):
            raise HTTPException(422, "asset_tier_map은 객체여야 해요.")
        for k, v in incoming.items():
            if v not in ("minimal", "standard", "strong"):
                raise HTTPException(422, f"허용되지 않은 등급: {v} (minimal/standard/strong)")
            tmap[k] = v
    # judge_model_map 부분 병합 + 값 검증(haiku-4-5/sonnet-4-6/sonnet-5).
    jmap = dict(cur.judge_model_map)
    incoming_j = body.get("judge_model_map")
    if incoming_j is not None:
        if not isinstance(incoming_j, dict):
            raise HTTPException(422, "judge_model_map은 객체여야 해요.")
        for k, v in incoming_j.items():
            if v not in ("haiku-4-5", "sonnet-4-6", "sonnet-5"):
                raise HTTPException(422, f"허용되지 않은 judge 모델: {v} (haiku-4-5/sonnet-4-6/sonnet-5)")
            jmap[k] = v
    updated = ConsoleSettings(
        auto_scan=bool(body.get("auto_scan", cur.auto_scan)),
        auto_overlap_review=bool(
            body.get("auto_overlap_review", cur.auto_overlap_review)),
        auto_detect_rate=body.get("auto_detect_rate", cur.auto_detect_rate),
        updated_by=current_principal(request).principal_id,
        updated_at=_dt.datetime.now(_dt.timezone.utc).isoformat(),
        asset_tier_map=tmap,
        judge_model_map=jmap,
    )
    store.put_settings(updated)
    return updated.__dict__


@router.post("/api/governance/queue/{record_id}/scan", dependencies=[Depends(require_role(*CONSOLE_ROLES))])
def run_scan(record_id: str, body: dict, request: Request):
    # 비동기: running 레코드를 즉시 남기고 반환해요(실제 스캔은 백그라운드). 큐/상세는
    # status 폴링으로 running→done 전이를 감지해 진행중 표시·버튼 disabled 처리해요.
    trigger = body.get("trigger", "manual-queue")
    principal = current_principal(request).principal_id
    try:
        rec = get_scan_service().run_async(
            record_id,
            trigger=trigger,
            principal=principal,
        )
    except ScanNotApplicableError as exc:
        raise HTTPException(409, str(exc)) from exc
    return rec.__dict__


@router.post("/api/governance/queue/{record_id}/overlap-review",
             dependencies=[Depends(require_role(*CONSOLE_ROLES))])
def run_overlap_review_now(record_id: str, request: Request):
    """중복검토를 즉시 수행해요(관리자 수동 트리거).

    스캔과 달리 동기예요 — Registry 읽기 + 순수 계산이라 수 초 안에 끝나고 백그라운드
    상태 전이를 폴링할 이유가 없어요. 자동 실행(등록 훅)과 같은 함수를 쓰므로 결과 형태도
    같아요.

    대상 레코드를 읽지 못하면 함수가 `status="failed"` 레코드를 남기고 그걸 그대로 돌려줘요
    (미검토와 구분되도록). 그 저장까지 실패한 경우만 None이라 404로 알려요.
    """
    from .overlap_review import overlap_view, run_overlap_review

    principal = current_principal(request).principal_id
    record = run_overlap_review(record_id, trigger="manual", principal=principal)
    if record is None:
        raise HTTPException(404, "중복검토 결과를 남기지 못했어요. 자산을 확인해 주세요.")
    return overlap_view(record_id)


@router.post("/api/governance/queue/{record_id}/gates/{tool_id}/rescan",
             dependencies=[Depends(require_role(*CONSOLE_ROLES))])
def rescan_gate(record_id: str, tool_id: str, body: dict, request: Request):
    """단일 게이트(도구)만 부분 재스캔해요 — running 레코드를 즉시 남기고 반환.

    base done scan에서 이 도구의 area만 갈아끼워요(다른 area 결과 보존). 부분 재스캔이
    부적합한 상황(base 없음/running/scan-level 에러, not_applicable/off 도구)은 rescan_tool이
    ValueError → 여기서 422로 변환해요. 반환은 run_scan과 같은 패턴(running rec.__dict__).
    """
    trigger = (body or {}).get("trigger", "manual-detail")
    principal = current_principal(request).principal_id
    try:
        rec = get_scan_service().rescan_tool(record_id, tool_id, trigger=trigger, principal=principal)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return rec.__dict__


@router.get("/api/governance/queue/{record_id}/scan/status", dependencies=[Depends(require_role(*CONSOLE_ROLES))])
def scan_status(record_id: str):
    store = get_gov_store()
    # stepfn 스캐너면 running 상태에서 SF 완료를 앱 done으로 전이(폴링 훅). 다른 스캐너는 무영향.
    # gates 경로와 동일 헬퍼를 공유해요(UI가 어느 쪽을 폴링하든 done 전이 보장).
    from .scan_service import sync_running_stepfn
    sync_running_stepfn(record_id)
    rec = store.latest_scan(record_id)
    if rec is None:
        return {"status": "none", "risk": None, "findings": []}
    return {"status": rec.status, "risk": rec.risk, "findings": rec.findings, "trigger": rec.trigger}
