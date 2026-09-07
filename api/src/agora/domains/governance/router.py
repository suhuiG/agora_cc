"""거버넌스 도메인 라우터 — 승인 워크플로우.

오너: 거버넌스 도메인. 자산 상태 전이(APPROVED/REJECTED/DEPRECATED)는 거버넌스의
배포 게이트예요. 카탈로그 SoT는 shared.deps 접근자로만 접근해요.

후속: 보안 정책 토글, 사용량·예산 대시보드 라우트가 여기 추가돼요.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ...shared.deps import (
    get_asset_responsibility_port,
    get_current_principal,
    get_registry,
    get_registry_id,
)
from ...shared.responsibility import ResponsibilityIncomplete
from ..catalog.registry.models import RecordStatus
from ..catalog.schemas import StatusUpdateRequest
from .categories_router import router as _categories_router
from .console_router import router as _console_router
from .dashboard_router import router as _dashboard_router
from .inventory_router import router as _inventory_router
from .queue_router import router as _queue_router
from .tiers_router import router as _tiers_router
from .tools_router import router as _tools_router

router = APIRouter(tags=["governance"])


@router.patch("/api/assets/{record_id}/status")
def update_asset_status(
    record_id: str,
    req: StatusUpdateRequest,
    request: Request,
):
    """Admin이 자산 상태를 변경해요 (APPROVED/REJECTED/DEPRECATED)."""
    from ..catalog.registry.models import InvalidStateTransition, RecordNotFound
    if not get_current_principal(request).is_admin:
        raise HTTPException(403, "자산 상태 변경은 admin 전용이에요.")
    registry = get_registry()
    registry_id = get_registry_id()
    try:
        target = RecordStatus(req.status)
    except ValueError:
        raise HTTPException(400, f"Invalid status: {req.status}")
    if target is RecordStatus.APPROVED:
        try:
            get_asset_responsibility_port().require_for_approval(record_id)
        except RecordNotFound:
            raise HTTPException(404, "Asset not found")
        except ResponsibilityIncomplete as exc:
            raise HTTPException(
                422,
                {
                    "message": (
                        "production 승인을 위해 책임자 연락 계약을 완성해야 해요."
                    ),
                    "blocking_reasons": list(
                        exc.status.blocking_reasons
                    ),
                },
            )
    try:
        new_status = registry.update_status(registry_id, record_id, target, req.reason)
    except RecordNotFound:
        raise HTTPException(404, "Asset not found")
    except InvalidStateTransition as e:
        raise HTTPException(422, str(e))
    return {"record_id": record_id, "status": new_status.value, "message": "상태가 변경됐어요."}

router.include_router(_console_router)
router.include_router(_tools_router)
router.include_router(_tiers_router)
router.include_router(_queue_router)
router.include_router(_dashboard_router)
router.include_router(_inventory_router)
router.include_router(_categories_router)
