"""로컬 폴러 ON/OFF API — **임시 기능이에요.** GA 때 이 파일과 라우터 등록을 지워요.

단일 출처는 `shared/dev_poller_switch.py` 의 모듈 docstring 이에요. 여기서는 그 스위치를
HTTP 로 노출만 해요.

**로컬 백엔드의 폴러 task 만** 다뤄요. 포털 ECS `desiredCount` 는 건드리지 않아요.
`AGORA_ROLE != local` 에서는 조회만 되고 변경은 409 예요 — 폴러 소유권이 포털에 있다는
사실을 화면이 그대로 읽을 수 있게, 거부를 조용히 하지 않고 이유를 실어 보내요.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...shared import dev_poller_switch

router = APIRouter()


class DevPollerState(BaseModel):
    role: str
    running: bool
    togglable: bool
    reason: str
    owned_by_env: bool


class DevPollerRequest(BaseModel):
    enabled: bool


# `async def` 여야 해요 — 스위치가 `asyncio.create_task` 를 쓰는데, FastAPI 는 sync
# 핸들러를 워커 스레드에서 돌려서 거기엔 running loop 이 없어요
# (2026-08-29 실측: `RuntimeError: no running event loop`).
@router.get("/api/dev/poller", response_model=DevPollerState)
async def get_dev_poller() -> DevPollerState:
    """폴러 상태와 **바꿀 수 없으면 그 이유**를 함께 돌려줘요."""
    return DevPollerState(**dev_poller_switch.state())


@router.put("/api/dev/poller", response_model=DevPollerState)
async def put_dev_poller(req: DevPollerRequest) -> DevPollerState:
    try:
        state = (
            dev_poller_switch.enable() if req.enabled
            else dev_poller_switch.disable()
        )
    except PermissionError as exc:
        # 409 — 권한(403)이 아니라 이 역할에서 성립하지 않는 상태 전이예요.
        raise HTTPException(409, str(exc)) from exc
    return DevPollerState(**state)
