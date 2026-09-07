"""도구 레지스트리 조회 (§3-M3). 도구 메타=코드 상수, 배포 여부=AWS 실시간 열거.

도구 목록은 런타임에 변하지 않아요(등록/편집/삭제 없음). 메타는 tool_catalog 코드
상수가 정본, "배포됐나"는 deployment_probe가 AWS에서 best-effort로 열거해요.

쓰기(create/patch/delete/classify/version/test/promote) 엔드포인트는 전부 제거했어요.
목록이 불변임을 명시하려고, 개별 도구 경로는 GET-only 스텁으로만 등록해 어떤 쓰기
메서드든 405(Method Not Allowed)로 거절해요(404가 아니라 "이 경로는 변경 불가"를 뜻해요).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ...shared.config import load_config
from .authz import CONSOLE_ROLES, require_role
from .deployment_probe import list_deployed_tool_ids
from .tool_catalog import list_catalog_tools

router = APIRouter(tags=["governance-tools"])


@router.get("/api/governance/tools", dependencies=[Depends(require_role(*CONSOLE_ROLES))])
def list_tools():
    """도구 메타(코드 상수) + 실배포 여부(AWS 열거)를 병합해 반환해요."""
    cfg = load_config()
    # 스캔 도구 Lambda/ECS(agora-tool-*)는 서울(scan_region)에 배포돼 있어요.
    # cfg.region(us-east-1)은 Registry 전용이라 여기서 열거하면 전부 "미배포"로 떠요(#5 버그).
    deployed = list_deployed_tool_ids(cfg.scan_region, cfg.stage)
    out = []
    for t in list_catalog_tools():
        d = t.__dict__.copy()
        d["deployed"] = t.tool_id in deployed
        out.append(d)
    return {"tools": out}


# 개별 도구 경로는 GET-only 스텁으로만 존재해요 — 목록이 불변이라 편집/삭제/버전 조작
# 경로가 없어요. 쓰기 메서드(POST/PATCH/DELETE)는 라우팅 계층에서 405로 거절돼요.
@router.get("/api/governance/tools/{tool_id}", dependencies=[Depends(require_role(*CONSOLE_ROLES))])
@router.get("/api/governance/tools/{tool_id}/{action}", dependencies=[Depends(require_role(*CONSOLE_ROLES))])
def _immutable(tool_id: str, action: str | None = None):
    raise HTTPException(405, "도구 목록은 불변이에요 — 편집/버전 조작 엔드포인트는 없어요.")
